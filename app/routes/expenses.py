"""
Add/edit/soft-delete/restore expenses. Equal splits call money.py's split_equal;
exact splits call money.py's validate_exact. money.py is DB-free by design
(see its module docstring), so the payer/participant group-membership check
lives here, not there.

The add and edit forms validate identically and render the same template, so
both share _validated_expense and _render_form below.
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
from app.queries import (
    balance_inputs,
    expenses_for_group,
    format_cents,
    get_members,
    used_categories,
)

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# $1,000,000 sanity ceiling on a single expense — see CLAUDE.md's money
# parsing note. Anything above this is almost certainly a typo (missing a
# decimal point), not a real trip expense.
_MAX_AMOUNT_CENTS = 100_000_000

# Matches the maxlength on the form's custom-category input.
_MAX_CATEGORY_LEN = 40

# The category <select>'s "Custom…" option, which reveals a text field for a
# name that isn't on the list yet. Rejected as a category name below, so it
# can never also be a stored category — an option whose value is the sentinel
# would be read back as "Custom…" on the next render.
_CUSTOM_CATEGORY = "__custom__"

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


def _chosen_category(form, known: list[str]) -> str | None:
    """Read the category picker: a <select> of the group's existing categories,
    plus a text field used when its "Custom…" option is chosen. A new group
    starts with an empty list — there are no built-in categories.

    Blank -> None (uncategorized); a listed name -> itself; the sentinel -> the
    name typed alongside it, folded onto an existing spelling so "food" joins
    yesterday's "Food" rather than opening a second tab next to it.

    The select is checked against `known` for the same reason payer_id is
    checked against the roster: a <select> constrains the browser, not a
    hand-written POST."""
    chosen = (form.get("category") or "").strip()
    if not chosen:
        return None
    if chosen != _CUSTOM_CATEGORY:
        if chosen not in known:
            raise ValueError("Choose a category from the list.")
        return chosen

    # The second .strip() matters: truncating can leave a trailing space, which
    # would make an otherwise-identical category miss its own tab.
    name = (form.get("custom_category") or "").strip()[:_MAX_CATEGORY_LEN].strip()
    if not name:
        raise ValueError("Enter a name for the custom category.")
    if name == _CUSTOM_CATEGORY:
        raise ValueError("That category name is reserved — pick another.")
    for option in known:
        if option.casefold() == name.casefold():
            return option
    return name


def _validated_expense(
    form, member_ids: set[int], known_categories: list[str]
) -> tuple[str, int, str, int, dict[int, int], str | None]:
    """Validate the expense form — add and edit submit identical fields.
    Returns (description, total_cents, split_type, payer_id, shares, category),
    or raises ValueError with a message ready to show the member. Everything this calls
    (parse_amount_cents, split_equal, validate_exact) already raises ValueError
    the same way, so the caller catches one exception type for the lot."""
    description = (form.get("description") or "").strip()
    if not description:
        raise ValueError("Enter a description.")

    total_cents = parse_amount_cents(form.get("amount", ""))

    split_type = form.get("split_type", "")
    if split_type not in ("equal", "exact"):
        raise ValueError("Choose a split type.")

    participant_raw = form.getlist("participant_ids")
    if not all(v.isdigit() for v in participant_raw):
        raise ValueError("Invalid participant selected.")
    participant_ids = [int(v) for v in participant_raw]
    if not participant_ids:
        raise ValueError("Select at least one participant.")
    # split_equal raises on duplicates, but an exact split would silently
    # collapse them into one share dict key, so reject for both split types.
    if len(set(participant_ids)) != len(participant_ids):
        raise ValueError("Duplicate participant selected.")

    payer_raw = form.get("payer_id", "")
    if not payer_raw.isdigit():
        raise ValueError("Choose who paid.")
    payer_id = int(payer_raw)

    # The payer and every participant must belong to this group. money.py is
    # DB-free by design (see its docstring) and does not do this check, so
    # it's the route's job — a member id from another group must be rejected.
    if payer_id not in member_ids or not set(participant_ids).issubset(member_ids):
        raise ValueError("Choose a payer and participants from this group.")

    if split_type == "equal":
        shares = split_equal(total_cents, participant_ids)
    else:
        shares = {pid: _parse_share_cents(form.get(f"share_{pid}", "")) for pid in participant_ids}
        validate_exact(total_cents, shares)

    return description, total_cents, split_type, payer_id, shares, _chosen_category(
        form, known_categories
    )


def _submitted_values(form, members: list) -> dict:
    """The field values as typed, echoed back into the template so a mistake in
    one field doesn't wipe the whole form."""
    return {
        "description": form.get("description", ""),
        "amount": form.get("amount", ""),
        "category": form.get("category", ""),
        "custom_category": form.get("custom_category", ""),
        "split_type": form.get("split_type", "equal"),
        "payer_id": form.get("payer_id", ""),
        "selected_ids": {int(v) for v in form.getlist("participant_ids") if v.isdigit()},
        "share_values": {m.id: form.get(f"share_{m.id}", "") for m in members},
    }


def _editable_expense(session: Session, group, expense_id: int) -> Expense | None:
    """The expense both edit routes are allowed to touch: this group's, and not
    soft-deleted — a deleted expense has to be restored before it can be
    edited, so an edit can't quietly resurrect one."""
    expense = session.get(Expense, expense_id)
    if expense is None or expense.group_id != group.id or expense.deleted_at is not None:
        return None
    return expense


@router.get("/g/{slug}/expenses/new")
def new_expense_form(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, me = result
    members = get_members(session, group.id)
    return templates.TemplateResponse(
        request,
        "expense_form.html",
        {
            "group": group,
            "members": members,
            "category_options": used_categories(session, group.id),
            "action": f"/g/{slug}/expenses/new",
            "heading": "Add expense",
            "submit_label": "Save expense",
            "description": "",
            "amount": "",
            "category": "",
            "custom_category": "",
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
    category_options = used_categories(session, group.id)
    form = await request.form()

    try:
        description, total_cents, split_type, payer_id, shares, category = _validated_expense(
            form, {m.id for m in members}, category_options
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "expense_form.html",
            {
                "group": group,
                "members": members,
                "action": f"/g/{slug}/expenses/new",
                "heading": "Add expense",
                "submit_label": "Save expense",
                "category_options": category_options,
                "error": str(exc),
                **_submitted_values(form, members),
            },
            status_code=400,
        )

    expense = Expense(
        group_id=group.id,
        description=description,
        amount_cents=total_cents,
        payer_id=payer_id,
        split_type=split_type,
        category=category,
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


@router.get("/g/{slug}/expenses/{expense_id}/edit")
def edit_expense_form(slug: str, expense_id: int, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result
    expense = _editable_expense(session, group, expense_id)
    if expense is None:
        return RedirectResponse(url=f"/g/{slug}", status_code=303)

    members = get_members(session, group.id)
    stored_shares = session.exec(select(Share).where(Share.expense_id == expense.id)).all()
    return templates.TemplateResponse(
        request,
        "expense_form.html",
        {
            "group": group,
            "members": members,
            "category_options": used_categories(session, group.id),
            "action": f"/g/{slug}/expenses/{expense.id}/edit",
            "heading": "Edit expense",
            "submit_label": "Save changes",
            "description": expense.description,
            "amount": format_cents(expense.amount_cents),
            "category": expense.category or "",
            "custom_category": "",
            "split_type": expense.split_type,
            # The template compares against `m.id|string`, so this has to be a
            # string here even though it's an int on the model.
            "payer_id": str(expense.payer_id),
            "selected_ids": {s.member_id for s in stored_shares},
            "share_values": {s.member_id: format_cents(s.amount_cents) for s in stored_shares},
        },
    )


@router.post("/g/{slug}/expenses/{expense_id}/edit")
async def update_expense(slug: str, expense_id: int, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result
    expense = _editable_expense(session, group, expense_id)
    if expense is None:
        return RedirectResponse(url=f"/g/{slug}", status_code=303)

    members = get_members(session, group.id)
    category_options = used_categories(session, group.id)
    form = await request.form()

    try:
        description, total_cents, split_type, payer_id, shares, category = _validated_expense(
            form, {m.id for m in members}, category_options
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            request,
            "expense_form.html",
            {
                "group": group,
                "members": members,
                "action": f"/g/{slug}/expenses/{expense.id}/edit",
                "heading": "Edit expense",
                "submit_label": "Save changes",
                "category_options": category_options,
                "error": str(exc),
                **_submitted_values(form, members),
            },
            status_code=400,
        )

    expense.description = description
    expense.amount_cents = total_cents
    expense.payer_id = payer_id
    expense.split_type = split_type
    # Assigned unconditionally, so clearing the field clears the category
    # rather than leaving the old one stuck on the expense.
    expense.category = category
    # created_at and created_by_id stay put: the feed is ordered by created_at,
    # so editing an expense must not jump it to the top of the activity list.
    session.add(expense)

    # Rewriting this expense's shares does not break the "shares are stored,
    # not recomputed" invariant — that rule stops the *roster* rewriting
    # history on read. This is one member deliberately restating one expense.
    for share in session.exec(select(Share).where(Share.expense_id == expense.id)).all():
        session.delete(share)
    session.commit()

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
    writer.writerow(["Date", "Description", "Category", "Amount", "Payer", "Split Type", "Participants & Shares"])
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
            expense.category or "",
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
