"""
Add/soft-delete/restore expenses. Equal splits call money.py's split_equal;
exact splits call money.py's validate_exact. money.py is DB-free by design
(see its module docstring), so the payer/participant group-membership check
lives here, not there.
"""
import csv
import io
import re
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import is_redirect, require_member
from app.config import TEMPLATES_DIR
from app.db import get_session
from app.money import net_balances, split_equal, validate_exact
from app.models import Expense, Settlement, Share
from app.queries import balance_inputs, expenses_for_group, format_cents, get_members

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# $1,000,000 sanity ceiling on a single expense — see CLAUDE.md's money
# parsing note. Anything above this is almost certainly a typo (missing a
# decimal point), not a real trip expense.
_MAX_AMOUNT_CENTS = 100_000_000

# Decimal() accepts far more than a money field should: "1e5" is a valid
# Decimal worth $100,000, and "NaN"/"Infinity" parse without raising, so a
# later `cents <= 0` guard never catches them. Gate on plain digits first.
_AMOUNT_RE = re.compile(r"-?\d+(\.\d+)?")


def parse_amount_cents(raw: str) -> int:
    """Decimal string -> integer cents. Never float() anywhere in this app.
    Rejects blank/non-numeric input, zero, negatives, and anything past the
    sanity ceiling — always with a readable ValueError, never a 500."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("Enter an amount.")
    if not _AMOUNT_RE.fullmatch(raw):
        raise ValueError("Enter a valid amount.")
    try:
        cents = int((Decimal(raw) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError):
        raise ValueError("Enter a valid amount.")
    if cents <= 0:
        raise ValueError("Amount must be greater than zero.")
    if cents > _MAX_AMOUNT_CENTS:
        raise ValueError("Amount is too large (over $1,000,000).")
    return cents


def _parse_share_cents(raw: str) -> int:
    """Same parsing as parse_amount_cents, but zero (and a blank field) is
    allowed — an exact split can include someone who skipped the appetizer."""
    raw = (raw or "").strip()
    if not raw:
        return 0
    if not _AMOUNT_RE.fullmatch(raw):
        raise ValueError("Enter a valid amount for each participant.")
    try:
        cents = int((Decimal(raw) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (DecimalException, ValueError):
        raise ValueError("Enter a valid amount for each participant.")
    if cents < 0:
        raise ValueError("Shares can't be negative.")
    return cents


@router.get("/g/{slug}/expenses/new")
def new_expense_form(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, me = result
    members = get_members(session, group.id)
    return templates.TemplateResponse(
        request,
        "expense_new.html",
        {
            "group": group,
            "members": members,
            "me": me,
            "description": "",
            "amount": "",
            "split_type": "equal",
            "payer_id": str(me.id),
            # Default everyone in — the common case at a group dinner.
            "selected_ids": {m.id for m in members},
            "share_values": {},
        },
    )


@router.post("/g/{slug}/expenses/new")
async def create_expense(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, me = result
    members = get_members(session, group.id)
    member_ids = {m.id for m in members}
    form = await request.form()

    def error(message: str):
        # Re-render with everything the member already typed so a mistake in
        # one field doesn't wipe the whole form.
        selected_ids = {int(v) for v in form.getlist("participant_ids") if v.isdigit()}
        share_values = {m.id: form.get(f"share_{m.id}", "") for m in members}
        return templates.TemplateResponse(
            request,
            "expense_new.html",
            {
                "group": group,
                "members": members,
                "me": me,
                "error": message,
                "description": form.get("description", ""),
                "amount": form.get("amount", ""),
                "split_type": form.get("split_type", "equal"),
                "payer_id": form.get("payer_id", ""),
                "selected_ids": selected_ids,
                "share_values": share_values,
            },
            status_code=400,
        )

    description = (form.get("description") or "").strip()
    if not description:
        return error("Enter a description.")

    try:
        total_cents = parse_amount_cents(form.get("amount", ""))
    except ValueError as exc:
        return error(str(exc))

    split_type = form.get("split_type", "")
    if split_type not in ("equal", "exact"):
        return error("Choose a split type.")

    participant_raw = form.getlist("participant_ids")
    if not all(v.isdigit() for v in participant_raw):
        return error("Invalid participant selected.")
    participant_ids = [int(v) for v in participant_raw]
    if not participant_ids:
        return error("Select at least one participant.")
    # split_equal raises on duplicates, but an exact split would silently
    # collapse them into one share dict key, so reject for both split types.
    if len(set(participant_ids)) != len(participant_ids):
        return error("Duplicate participant selected.")

    payer_raw = form.get("payer_id", "")
    if not payer_raw.isdigit():
        return error("Choose who paid.")
    payer_id = int(payer_raw)

    # The payer and every participant must belong to this group. money.py is
    # DB-free by design (see its docstring) and does not do this check, so
    # it's the route's job — a member id from another group must be rejected.
    if payer_id not in member_ids or not set(participant_ids).issubset(member_ids):
        return error("Choose a payer and participants from this group.")

    try:
        if split_type == "equal":
            shares = split_equal(total_cents, participant_ids)
        else:
            shares = {pid: _parse_share_cents(form.get(f"share_{pid}", "")) for pid in participant_ids}
            validate_exact(total_cents, shares)
    except ValueError as exc:
        return error(str(exc))

    expense = Expense(
        group_id=group.id,
        description=description,
        amount_cents=total_cents,
        payer_id=payer_id,
        split_type=split_type,
        created_by_id=me.id,  # me came from require_member, already scoped to this group
    )
    session.add(expense)
    session.commit()
    session.refresh(expense)

    # Shares are stored, not recomputed — a member joining mid-trip must not
    # silently rewrite the history of expenses they weren't part of.
    for member_id, amount_cents in shares.items():
        session.add(Share(expense_id=expense.id, member_id=member_id, amount_cents=amount_cents))
    session.commit()

    return RedirectResponse(url=f"/g/{slug}", status_code=303)


@router.post("/g/{slug}/expenses/{expense_id}/delete")
def delete_expense(slug: str, expense_id: int, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result
    expense = session.get(Expense, expense_id)
    if expense is not None and expense.group_id == group.id and expense.deleted_at is None:
        expense.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(expense)
        session.commit()
    return RedirectResponse(url=f"/g/{slug}", status_code=303)


@router.post("/g/{slug}/expenses/{expense_id}/restore")
def restore_expense(slug: str, expense_id: int, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result
    expense = session.get(Expense, expense_id)
    if expense is not None and expense.group_id == group.id and expense.deleted_at is not None:
        expense.deleted_at = None
        session.add(expense)
        session.commit()
    return RedirectResponse(url=f"/g/{slug}", status_code=303)


@router.get("/g/{slug}/export.csv")
def export_csv(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result

    output = io.StringIO()
    writer = csv.writer(output)

    # Expenses section
    writer.writerow(["Expenses"])
    writer.writerow(["Date", "Description", "Amount", "Payer", "Split Type", "Participants & Shares"])
    for row in expenses_for_group(session, group.id):
        expense = row["expense"]
        if expense.deleted_at is not None:
            continue
        shares_str = "; ".join(
            f"{s['name']}: {format_cents(s['amount_cents'])}"
            for s in row["shares"]
        )
        writer.writerow([
            expense.created_at.date().isoformat(),
            expense.description,
            format_cents(expense.amount_cents),
            row["payer_name"],
            expense.split_type,
            shares_str,
        ])

    # Blank row before settlements section
    writer.writerow([])
    writer.writerow(["Settlements"])
    writer.writerow(["Date", "From", "To", "Amount", "Note"])
    settlements = session.exec(
        select(Settlement)
        .where(Settlement.group_id == group.id, Settlement.deleted_at.is_(None))
        .order_by(Settlement.created_at.desc())
    ).all()
    members_by_id = {m.id: m.name for m in get_members(session, group.id)}
    for settlement in settlements:
        writer.writerow([
            settlement.created_at.date().isoformat(),
            members_by_id.get(settlement.from_member_id, "?"),
            members_by_id.get(settlement.to_member_id, "?"),
            format_cents(settlement.amount_cents),
            settlement.note or "",
        ])

    # Blank row before balances section
    writer.writerow([])
    writer.writerow(["Balances"])
    writer.writerow(["Member", "Balance"])
    expenses, shares, settlement_rows = balance_inputs(session, group.id)
    balances = net_balances(expenses, shares, settlement_rows)
    members = get_members(session, group.id)
    for member in sorted(members, key=lambda m: m.id):
        balance_cents = balances.get(member.id, 0)
        writer.writerow([
            member.name,
            format_cents(balance_cents),
        ])

    csv_data = output.getvalue()
    return StreamingResponse(
        iter([csv_data]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={group.slug}_expenses.csv"},
    )
