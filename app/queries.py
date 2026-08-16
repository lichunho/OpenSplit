"""
Read-side queries and formatting shared by more than one route module.

These live here rather than in whichever route happened to need them first:
groups, expenses and settlements all need the same roster and the same
balance inputs, and three private copies would drift apart silently.
"""
from sqlmodel import Session, select

from app.models import Expense, Member, Settlement, Share


def get_members(session: Session, group_id: int) -> list[Member]:
    # Group has no ORM relationship to Member (models.py defines none), so
    # the roster is queried explicitly wherever it's needed.
    return session.exec(select(Member).where(Member.group_id == group_id)).all()


def format_cents(cents: int) -> str:
    """Integer cents -> plain decimal string ("1234" -> "12.34"), no currency
    symbol. The sign is handled separately because Python floors toward
    negative infinity: -334 // 100 is -4 and -334 % 100 is 66, which would
    render a -$3.34 balance as "-4.66"."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return "{}{}.{:02d}".format(sign, cents // 100, cents % 100)


def balance_inputs(session: Session, group_id: int):
    """Query rows shaped exactly for money.net_balances's plain-tuple inputs,
    filtered to deleted_at IS NULL throughout.

    A soft-deleted expense's Share rows carry no deleted_at of their own, so
    they can't be filtered directly — Share is joined to Expense and the
    *expense's* deleted_at gates it. Forgetting this join would keep summing
    a deleted expense's shares while dropping only its paid-amount side,
    corrupting every balance by that expense's total.
    """
    expenses = session.exec(
        select(Expense.payer_id, Expense.amount_cents)
        .where(Expense.group_id == group_id, Expense.deleted_at.is_(None))
    ).all()
    shares = session.exec(
        select(Share.member_id, Share.amount_cents)
        .join(Expense, Share.expense_id == Expense.id)
        .where(Expense.group_id == group_id, Expense.deleted_at.is_(None))
    ).all()
    settlements = session.exec(
        select(Settlement.from_member_id, Settlement.to_member_id, Settlement.amount_cents)
        .where(Settlement.group_id == group_id, Settlement.deleted_at.is_(None))
    ).all()
    return expenses, shares, settlements


def expenses_for_group(session: Session, group_id: int) -> list[dict]:
    """Every expense for the group (active and soft-deleted), each paired with
    its payer's name and per-member shares, for the dashboard list."""
    expenses = session.exec(
        select(Expense).where(Expense.group_id == group_id).order_by(Expense.created_at.desc())
    ).all()
    members_by_id = {m.id: m.name for m in get_members(session, group_id)}
    rows = []
    for expense in expenses:
        shares = session.exec(select(Share).where(Share.expense_id == expense.id)).all()
        rows.append(
            {
                "expense": expense,
                "payer_name": members_by_id.get(expense.payer_id, "?"),
                "shares": [
                    {"name": members_by_id.get(s.member_id, "?"), "amount_cents": s.amount_cents}
                    for s in shares
                ],
            }
        )
    return rows
