"""
Import a previously exported CSV back into a group: upload, preview, confirm.

Parsing lives in csv_import.py (DB-free, like money.py); this module only
resolves names to member ids and writes rows. It is its own router rather than
part of expenses.py because one import creates expenses, settlements *and*
members — it belongs to none of the three.

Import appends. It never deletes or rewrites what the group already has, so
the same file imported twice creates duplicates; the preview screen is the
guard against that, and soft delete is the undo.
"""
from datetime import date, datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.auth import is_redirect, require_member
from app.config import TEMPLATES_DIR
from app.csv_import import ImportPlan, parse_export_csv
from app.db import get_session
from app.models import Expense, Member, Settlement, Share
from app.queries import get_members, used_categories

router = APIRouter()
templates = Jinja2Templates(directory=TEMPLATES_DIR)

# A group's whole history is a few hundred KB of CSV at most. The ceiling is
# here so a wrong file (a video, a database dump) is refused with a sentence
# instead of being parsed line by line inside a Lambda invocation.
_MAX_UPLOAD_BYTES = 2 * 1024 * 1024


def _form_response(request: Request, group, errors: list[str], status_code: int = 200):
    return templates.TemplateResponse(
        request,
        "import_form.html",
        {"group": group, "errors": errors},
        status_code=status_code,
    )


def _new_member_names(session: Session, group, plan: ImportPlan) -> list[str]:
    """The names in the file that aren't on the roster yet, in file order.
    Matched casefolded, the same rule groups.py uses when adding a member by
    hand — "chris" in the file is the existing Chris, not a second person."""
    existing = {m.name.casefold() for m in get_members(session, group.id)}
    return [name for name in plan.member_names if name.casefold() not in existing]


def _member_ids(session: Session, group, plan: ImportPlan) -> dict[str, int]:
    """Create the missing members, then return {casefolded name: member id}
    covering every name the file references."""
    for name in _new_member_names(session, group, plan):
        session.add(Member(group_id=group.id, name=name))
    session.commit()
    return {m.name.casefold(): m.id for m in get_members(session, group.id)}


def _stamp(when: date, used: dict[date, int]) -> datetime:
    """A row carries a date but no time — the export writes date only. Hand
    each row on a given date its own timestamp, one second apart, assigned in
    the order rows are written (oldest first). Without this every row on a day
    would share one midnight and the activity feed's `created_at desc` sort
    would order them arbitrarily instead of reproducing the file."""
    offset = used.get(when, 0)
    used[when] = offset + 1
    return datetime(when.year, when.month, when.day) + timedelta(seconds=offset)


def _fold_category(name: str | None, known: list[str]) -> str | None:
    """Fold a category onto an existing spelling so an imported "food" joins
    the group's "Food" tab rather than opening a second one next to it — the
    same rule _chosen_category applies on the expense form.

    _chosen_category itself is deliberately not reused: it also requires the
    name to be one the group has already used, which is right for a <select>
    and wrong for a file that may introduce new ones."""
    if name is None:
        return None
    for option in known:
        if option.casefold() == name.casefold():
            return option
    known.append(name)
    return name


def _count(number: int, noun: str) -> str:
    """"1 expense", "3 expenses" — the flash reads as a sentence either way."""
    return "{} {}{}".format(number, noun, "" if number == 1 else "s")


@router.get("/g/{slug}/import")
def import_form(slug: str, request: Request, result=Depends(require_member)):
    if is_redirect(result):
        return result
    group, _me = result
    return _form_response(request, group, errors=[])


@router.post("/g/{slug}/import/preview")
async def preview_import(
    slug: str,
    request: Request,
    file: UploadFile | None = None,
    result=Depends(require_member),
    session: Session = Depends(get_session),
):
    if is_redirect(result):
        return result
    group, _me = result

    if file is None or not file.filename:
        return _form_response(request, group, ["Choose a CSV file to import."], status_code=400)

    raw = await file.read()
    if len(raw) > _MAX_UPLOAD_BYTES:
        return _form_response(request, group, ["That file is too large (over 2 MB)."], status_code=400)
    try:
        # utf-8-sig, not utf-8: Excel writes a byte-order mark, and it would
        # otherwise land inside the first section title and hide it.
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _form_response(
            request, group, ["That file isn't text this can read — export it as UTF-8 CSV."], status_code=400
        )

    plan = parse_export_csv(text)
    if plan.errors:
        return _form_response(request, group, plan.errors, status_code=400)

    return templates.TemplateResponse(
        request,
        "import_preview.html",
        {
            "group": group,
            "plan": plan,
            "new_members": _new_member_names(session, group, plan),
            "csv_text": text,
        },
    )


@router.post("/g/{slug}/import/confirm")
async def confirm_import(
    slug: str,
    request: Request,
    result=Depends(require_member),
    session: Session = Depends(get_session),
):
    if is_redirect(result):
        return result
    group, me = result

    form = await request.form()
    # Re-parsed from scratch rather than trusted: the hidden field is just the
    # file carried across the two steps, and it reaches us through the browser.
    plan = parse_export_csv(form.get("csv_text") or "")
    if plan.errors:
        return _form_response(request, group, plan.errors, status_code=400)

    new_members = _new_member_names(session, group, plan)
    member_ids = _member_ids(session, group, plan)
    categories = used_categories(session, group.id)
    stamps: dict[date, int] = {}

    # Both loops run in reverse: the export writes newest-first, so reversed()
    # is oldest-first, which is the order _stamp needs to hand out its seconds.
    for parsed in reversed(plan.expenses):
        expense = Expense(
            group_id=group.id,
            description=parsed.description,
            amount_cents=parsed.amount_cents,
            payer_id=member_ids[parsed.payer.casefold()],
            split_type=parsed.split_type,
            category=_fold_category(parsed.category, categories),
            created_by_id=me.id,  # the file has no author column; the importer is the actor
            created_at=_stamp(parsed.date, stamps),
        )
        session.add(expense)
        # flush, not commit: this needs expense.id for the Share rows, but a
        # commit per row would be one round trip per expense on a pooled
        # connection. The whole import lands in one transaction below.
        session.flush()
        for name, amount_cents in parsed.shares:
            session.add(
                Share(
                    expense_id=expense.id,
                    member_id=member_ids[name.casefold()],
                    amount_cents=amount_cents,
                )
            )

    for parsed in reversed(plan.settlements):
        session.add(
            Settlement(
                group_id=group.id,
                from_member_id=member_ids[parsed.from_name.casefold()],
                to_member_id=member_ids[parsed.to_name.casefold()],
                amount_cents=parsed.amount_cents,
                created_by_id=me.id,
                note=parsed.note,
                created_at=_stamp(parsed.date, stamps),
            )
        )

    session.commit()

    parts = [_count(len(plan.expenses), "expense")]
    if plan.settlements:
        parts.append(_count(len(plan.settlements), "settlement"))
    if new_members:
        parts.append(_count(len(new_members), "new member"))
    flash = "Imported {}.".format(", ".join(parts))
    return RedirectResponse(url="/g/{}?flash={}".format(slug, quote(flash, safe="")), status_code=303)
