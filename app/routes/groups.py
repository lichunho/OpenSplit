"""
Create group, group dashboard (balances, simplified debts, merged activity
feed), and the when2meet-style identify/switch flow.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.auth import (
    clear_identity,
    hash_password,
    is_redirect,
    require_member,
    set_identity,
    verify_password,
)
from app.config import TEMPLATES_DIR
from app.db import get_session
from app.models import Expense, Group, Member, Settlement
from app.money import net_balances, simplify
from app.queries import balance_inputs, expenses_for_group, get_members, used_categories

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)


def _get_group_or_none(slug: str, session: Session) -> Group | None:
    return session.exec(select(Group).where(Group.slug == slug)).first()


def _name_taken(session: Session, group_id: int, name: str) -> bool:
    # The UniqueConstraint on (group_id, name) is case-sensitive, so "chris"
    # and "Chris" would both insert; this check catches that in code.
    return any(m.name.casefold() == name.casefold() for m in get_members(session, group_id))


def _activity_feed(session: Session, group_id: int, category: str | None = None) -> list[dict]:
    """Expenses and settlements merged into one list ordered by created_at
    (newest first) — the plan's activity feed. Soft-deleted rows of either
    kind stay in the list (the template strikes them through with a restore
    control); only balance math excludes them.

    `category` narrows the feed and nothing else: None shows everything, a
    name shows that category, and "" shows the uncategorized ones. Balances
    are computed from balance_inputs, which never sees this filter — what
    someone owes can't depend on which tab you happen to be looking at."""
    expense_items = [
        {"type": "expense", "created_at": row["expense"].created_at, **row}
        for row in expenses_for_group(session, group_id)
    ]
    members_by_id = {m.id: m.name for m in get_members(session, group_id)}
    settlements = session.exec(
        select(Settlement).where(Settlement.group_id == group_id).order_by(Settlement.created_at.desc())
    ).all()
    settlement_items = [
        {
            "type": "settlement",
            "created_at": s.created_at,
            "settlement": s,
            "from_name": members_by_id.get(s.from_member_id, "?"),
            "to_name": members_by_id.get(s.to_member_id, "?"),
        }
        for s in settlements
    ]
    if category is not None:
        expense_items = [
            item for item in expense_items if (item["expense"].category or "") == category
        ]
        # A settlement has no category, so it belongs to no tab but "All".
        settlement_items = []

    return sorted(expense_items + settlement_items, key=lambda item: item["created_at"], reverse=True)


@router.get("/")
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@router.post("/groups")
def create_group(request: Request, name: str = Form(...), session: Session = Depends(get_session)):
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            request, "index.html", {"error": "Group name can't be empty."}, status_code=400
        )
    group = Group(name=name)
    session.add(group)
    session.commit()
    session.refresh(group)
    return RedirectResponse(url=f"/g/{group.slug}", status_code=303)


@router.get("/g/{slug}")
def group_dashboard(slug: str, request: Request, result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, member = result
    members = get_members(session, group.id)

    # Three states, and no sentinel value needed for the third: a category can
    # never be the empty string (_chosen_category maps blank to None), so
    # absent -> everything, "" -> uncategorized only, "Food" -> that category.
    active_category = request.query_params.get("category")
    feed = _activity_feed(session, group.id, active_category)
    # Whether the Uncategorized tab is worth rendering at all. Asked as its own
    # one-column query rather than off `feed`, which the filter above may have
    # already narrowed to a single category.
    has_uncategorized = session.exec(
        select(Expense.id).where(Expense.group_id == group.id, Expense.category.is_(None))
    ).first() is not None

    expenses, shares, settlements = balance_inputs(session, group.id)
    balances = net_balances(expenses, shares, settlements)
    # Every current member gets a row, even one with zero activity so far —
    # net_balances only returns members that appear in its inputs.
    balances_by_member = {m.id: balances.get(m.id, 0) for m in members}
    balance_rows = [
        {"name": m.name, "is_me": m.id == member.id, "cents": balances_by_member[m.id]}
        for m in members
    ]

    # simplify() only ever looks at sign, so the zero-filled dict above is an
    # equivalent input to the raw net_balances() result for this call.
    members_by_id = {m.id: m.name for m in members}
    transfer_rows = [
        {
            "debtor_name": "You" if debtor_id == member.id else members_by_id.get(debtor_id, "?"),
            "creditor_name": "You" if creditor_id == member.id else members_by_id.get(creditor_id, "?"),
            "amount_cents": amount_cents,
        }
        for debtor_id, creditor_id, amount_cents in simplify(balances_by_member)
    ]

    return templates.TemplateResponse(
        request,
        "group.html",
        {
            "group": group,
            "members": members,
            "me": member,
            "activity": feed,
            "active_category": active_category,
            "used_categories": used_categories(session, group.id),
            "has_uncategorized": has_uncategorized,
            # Only rendered under an active filter, so it reads as "the Food
            # tab totals $412.50", never as a balance.
            "filtered_total_cents": sum(
                item["expense"].amount_cents
                for item in feed
                if item["type"] == "expense" and item["expense"].deleted_at is None
            ),
            "balances": balance_rows,
            "transfers": transfer_rows,
            # Carried on the settle-up redirect as a query param, not session
            # state — settlements.py builds the message, this just displays
            # whatever arrives (absent on every other route into this page).
            "flash": request.query_params.get("flash"),
        },
    )


@router.get("/g/{slug}/identify")
def identify_form(slug: str, request: Request, session: Session = Depends(get_session)):
    group = _get_group_or_none(slug, session)
    if group is None:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        request, "identify.html", {"group": group, "members": get_members(session, group.id)}
    )


@router.post("/g/{slug}/identify")
def identify_submit(
    slug: str,
    request: Request,
    existing_member_id: str = Form(""),
    new_name: str = Form(""),
    password: str = Form(""),
    session: Session = Depends(get_session),
):
    group = _get_group_or_none(slug, session)
    if group is None:
        return RedirectResponse(url="/", status_code=303)

    def error(message: str):
        return templates.TemplateResponse(
            request,
            "identify.html",
            {"group": group, "members": get_members(session, group.id), "error": message},
            status_code=400,
        )

    if existing_member_id:
        # The form value arrives as a string from an untrusted client, so a
        # non-numeric one must render the error page, not raise out of int().
        member = None
        if existing_member_id.isdigit():
            member = session.get(Member, int(existing_member_id))
        if member is None or member.group_id != group.id:
            return error("That member no longer exists.")

        if member.password_hash is not None:
            # Wrong password re-renders the form with a readable error —
            # never a 500 — per CLAUDE.md's auth-flow verification step.
            if not verify_password(password, member.password_hash):
                return error("Wrong password.")
        elif password:
            # No password set yet: offered the chance to set one now.
            member.password_hash = hash_password(password)
            session.add(member)
            session.commit()

        set_identity(request, group.id, member.id)
        return RedirectResponse(url=f"/g/{slug}", status_code=303)

    new_name = new_name.strip()
    if not new_name:
        return error("Enter your name.")
    if _name_taken(session, group.id, new_name):
        return error("That name is already taken — pick yourself from the list instead.")

    member = Member(
        group_id=group.id,
        name=new_name,
        password_hash=hash_password(password) if password else None,
    )
    session.add(member)
    session.commit()
    session.refresh(member)
    set_identity(request, group.id, member.id)
    return RedirectResponse(url=f"/g/{slug}", status_code=303)


@router.post("/g/{slug}/switch")
def switch(slug: str, request: Request, session: Session = Depends(get_session)):
    group = _get_group_or_none(slug, session)
    if group is not None:
        clear_identity(request, group.id)
    return RedirectResponse(url=f"/g/{slug}/identify", status_code=303)


@router.post("/g/{slug}/members")
def add_member(slug: str, name: str = Form(...), result=Depends(require_member), session: Session = Depends(get_session)):
    if is_redirect(result):
        return result
    group, _me = result

    name = name.strip()
    if not name or _name_taken(session, group.id, name):
        # Minimal-scope route: no error UI for this edge case, just no-op
        # back to the dashboard. Full validation UX is out of scope here.
        return RedirectResponse(url=f"/g/{slug}", status_code=303)

    session.add(Member(group_id=group.id, name=name))
    session.commit()
    return RedirectResponse(url=f"/g/{slug}", status_code=303)
