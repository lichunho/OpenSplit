"""
The three-screen settle-up flow (who are you paying -> confirm amount ->
record it) plus soft-delete/restore undo. Settlement is its own table (see
models.py) — two parties, no shares — so this module stays independent of
expenses.py's split logic and only borrows its amount parser.
"""
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import is_redirect, require_member
from app.config import TEMPLATES_DIR
from app.db import get_session
from app.money import net_balances, simplify
from app.models import Member, Settlement

# Both are reused rather than re-implemented: parse_amount_cents owns the
# strict digits-only gate that rejects "1e5"/"NaN"/"Infinity", and
# balance_inputs owns the Share->Expense join that respects soft delete.
# Duplicating either is how the two would silently drift apart.
from app.routes.expenses import parse_amount_cents
from app.routes.groups import balance_inputs

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _get_members(session: Session, group_id: int) -> list[Member]:
    # Same duplication expenses.py already carries relative to groups.py (see
    # its own _get_members comment): no ORM relationship exists to walk, and
    # each route module keeps its own copy of this query rather than reaching
    # into a sibling module's internals for it.
    return session.exec(select(Member).where(Member.group_id == group_id)).all()


def _format_amount(cents: int) -> str:
    """Integer cents -> plain decimal string ("1234" -> "12.34"), no currency
    symbol — used for form field values, where the label already shows it."""
    return "{}.{:02d}".format(cents // 100, cents % 100)


def _suggested_transfers(session: Session, group_id: int, members: list[Member]):
    """This group's simplify() output, keyed the same way group_dashboard
    builds it: every current member gets a zero-filled balance row first, so
    simplify() (which only looks at sign) sees a stable member set."""
    expenses, shares, settlements = balance_inputs(session, group_id)
    balances = net_balances(expenses, shares, settlements)
    balances_by_member = {m.id: balances.get(m.id, 0) for m in members}
    return simplify(balances_by_member)


@router.get("/g/{slug}/settle")
def settle_pick(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, me = result
    members = _get_members(session, group.id)
    members_by_id = {m.id: m for m in members}

    transfers = _suggested_transfers(session, group.id, members)
    suggested = [
        {"member_id": creditor_id, "name": members_by_id[creditor_id].name, "amount_cents": amount_cents}
        for debtor_id, creditor_id, amount_cents in transfers
        if debtor_id == me.id
    ]
    # The "someone else" list is every other member, visible and unfiltered —
    # not just the ones simplify() suggested. Paying Sam directly when the
    # optimal route went through Alex is a normal event (plan is explicit
    # this must not be collapsed behind a toggle).
    others = [m for m in members if m.id != me.id]

    return templates.TemplateResponse(
        request,
        "settle_pick.html",
        {"group": group, "me": me, "suggested": suggested, "others": others},
    )


@router.get("/g/{slug}/settle/{member_id}")
def settle_confirm(
    slug: str, member_id: int, request: Request, result=Depends(require_member), session: Session = Depends(get_session)
):
    if is_redirect(result):
        return result
    group, me = result
    members = _get_members(session, group.id)
    payee = next((m for m in members if m.id == member_id), None)
    if payee is None or payee.id == me.id:
        # Not a real payee in this group (foreign id, stale link, or trying
        # to pay yourself) — bounce back to the picker rather than 404/500.
        return RedirectResponse(url=f"/g/{slug}/settle", status_code=303)

    transfers = _suggested_transfers(session, group.id, members)
    suggested_cents = next(
        (amount_cents for debtor_id, creditor_id, amount_cents in transfers
         if debtor_id == me.id and creditor_id == member_id),
        0,
    )

    if suggested_cents > 0:
        amount = _format_amount(suggested_cents)
        hint = "Settles you up with {}.".format(payee.name)
    else:
        # Not one of simplify()'s suggested transfers (a "someone else" pick)
        # — nothing to pre-fill or claim credit for; the member types their
        # own figure.
        amount = ""
        hint = ""

    return templates.TemplateResponse(
        request,
        "settle_confirm.html",
        {
            "group": group,
            "members": members,
            "me": me,
            "payee_name": payee.name,
            "to_member_id": payee.id,
            "amount": amount,
            "payer_id": str(me.id),
            "note": "",
            "suggested_cents": suggested_cents,
            "hint": hint,
            "error": None,
        },
    )


@router.post("/g/{slug}/settle")
async def create_settlement(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, me = result
    members = _get_members(session, group.id)
    members_by_id = {m.id: m for m in members}
    form = await request.form()

    def error(message: str, payee=None):
        # Re-render the confirm screen (400, not a redirect) with whatever
        # was submitted, matching expenses.py's error-closure pattern.
        to_raw = form.get("to_member_id", "")
        return templates.TemplateResponse(
            request,
            "settle_confirm.html",
            {
                "group": group,
                "members": members,
                "me": me,
                "payee_name": payee.name if payee else "that person",
                "to_member_id": payee.id if payee else to_raw,
                "amount": form.get("amount", ""),
                "payer_id": form.get("from_member_id", ""),
                "note": form.get("note", ""),
                "suggested_cents": 0,
                "hint": "",
                "error": message,
            },
            status_code=400,
        )

    to_raw = form.get("to_member_id", "")
    payee = members_by_id.get(int(to_raw)) if to_raw.isdigit() else None
    if payee is None:
        return error("Choose who you're paying.")

    from_raw = form.get("from_member_id", "")
    payer = members_by_id.get(int(from_raw)) if from_raw.isdigit() else None
    if payer is None:
        return error("Choose who paid.", payee=payee)

    if payer.id == payee.id:
        return error("Payer and payee must be different people.", payee=payee)

    try:
        amount_cents = parse_amount_cents(form.get("amount", ""))
    except ValueError as exc:
        return error(str(exc), payee=payee)

    note = (form.get("note") or "").strip() or None

    settlement = Settlement(
        group_id=group.id,
        from_member_id=payer.id,
        to_member_id=payee.id,
        amount_cents=amount_cents,
        created_by_id=me.id,  # me came from require_member, already scoped to this group
        note=note,
    )
    session.add(settlement)
    session.commit()

    # Flash copy is carried as a query param on the redirect target, read by
    # group_dashboard — no session/flash infra to touch (auth.py is out of
    # scope for this milestone). It adapts to which side of the ledger "me"
    # is on, since the payer is a dropdown and needn't be the current member.
    amount_str = "{}{}".format(group.currency_symbol, _format_amount(amount_cents))
    if payer.id == me.id:
        flash = "Recorded: you paid {} {}.".format(payee.name, amount_str)
    elif payee.id == me.id:
        flash = "Recorded: {} paid you {}.".format(payer.name, amount_str)
    else:
        flash = "Recorded: {} paid {} {}.".format(payer.name, payee.name, amount_str)

    return RedirectResponse(url="/g/{}?flash={}".format(slug, quote(flash, safe="")), status_code=303)


@router.post("/g/{slug}/settlements/{settlement_id}/delete")
def delete_settlement(slug: str, settlement_id: int, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result
    settlement = session.get(Settlement, settlement_id)
    if settlement is not None and settlement.group_id == group.id and settlement.deleted_at is None:
        settlement.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
        session.add(settlement)
        session.commit()
    return RedirectResponse(url=f"/g/{slug}", status_code=303)


@router.post("/g/{slug}/settlements/{settlement_id}/restore")
def restore_settlement(slug: str, settlement_id: int, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result
    settlement = session.get(Settlement, settlement_id)
    if settlement is not None and settlement.group_id == group.id and settlement.deleted_at is not None:
        settlement.deleted_at = None
        session.add(settlement)
        session.commit()
    return RedirectResponse(url=f"/g/{slug}", status_code=303)
