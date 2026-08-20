"""
Parse an exported CSV back into a plan of rows to create.

Text in, dataclasses out: no Session, no models, no queries, so tests can hit
this directly the way they hit money.py. Resolving names to member ids and
writing anything is routes/imports.py's job.

Columns are read by name, never by position. The export's shape keeps moving —
Category arrived after the first release and more will follow — so each
section's header row becomes a {name: index} map, the columns needed here are
looked up in it, and every other column in the file is ignored. A future
export that adds or reorders columns keeps importing with no change here.

Every bad row is reported, not just the first: one typo in row 3 must not hide
the twelve good rows behind it, and a member re-typing a whole file to find
errors one at a time would give up.
"""
import csv
import io
from dataclasses import dataclass, field
from datetime import date

from app.money import parse_amount_cents, parse_share_cents, validate_exact

# Both mirror a maxlength in the templates: the custom-category input in
# expense_form.html and the add-member input in group.html.
_MAX_CATEGORY_LEN = 40
_MAX_NAME_LEN = 80

# Section titles, as the export writes them (matched casefolded). Balances is
# recognised so its rows can be skipped: it is derived from the other two, and
# importing it would double-count every expense in the file.
_EXPENSES = "expenses"
_SETTLEMENTS = "settlements"
_BALANCES = "balances"
_SECTIONS = (_EXPENSES, _SETTLEMENTS, _BALANCES)

# Display spellings, matched casefolded. Category and Note are optional, so a
# file exported before they existed still imports.
_EXPENSE_REQUIRED = ("Date", "Description", "Amount", "Payer", "Split Type", "Participants & Shares")
_SETTLEMENT_REQUIRED = ("Date", "From", "To", "Amount")


@dataclass
class ParsedExpense:
    row: int
    date: date
    description: str
    category: str | None
    amount_cents: int
    payer: str
    split_type: str
    shares: list[tuple[str, int]]  # (member name, cents), in file order


@dataclass
class ParsedSettlement:
    row: int
    date: date
    from_name: str
    to_name: str
    amount_cents: int
    note: str | None


@dataclass
class ImportPlan:
    expenses: list[ParsedExpense] = field(default_factory=list)
    settlements: list[ParsedSettlement] = field(default_factory=list)
    member_names: list[str] = field(default_factory=list)  # every name referenced, first-seen
    errors: list[str] = field(default_factory=list)


def parse_export_csv(text: str) -> ImportPlan:
    """CSV text -> an ImportPlan. Never raises: a file this can't read comes
    back as a plan whose `errors` list is non-empty and whose row lists are
    empty, which is what the preview screen renders."""
    plan = ImportPlan()
    seen_names: dict[str, str] = {}  # casefolded -> the spelling first seen
    section = None
    columns = None  # the current section's {casefolded name: index}, once its header row is read
    saw_section = False

    for number, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        title = _section_title(row)
        if title is not None:
            section, columns, saw_section = title, None, True
            continue
        if _is_blank(row):
            # The export separates its sections with a blank row; anything
            # after one is unclaimed until the next title.
            section, columns = None, None
            continue
        if section is None or section == _BALANCES:
            continue

        if columns is None:
            columns = {cell.strip().casefold(): index for index, cell in enumerate(row)}
            required = _EXPENSE_REQUIRED if section == _EXPENSES else _SETTLEMENT_REQUIRED
            missing = [name for name in required if name.casefold() not in columns]
            if missing:
                plan.errors.append(
                    "The {} section is missing these columns: {}.".format(
                        section.capitalize(), ", ".join(missing)
                    )
                )
                section, columns = None, None
            continue

        try:
            if section == _EXPENSES:
                expense = _expense_row(number, row, columns)
                plan.expenses.append(expense)
                _remember(plan, seen_names, expense.payer)
                for name, _cents in expense.shares:
                    _remember(plan, seen_names, name)
            else:
                settlement = _settlement_row(number, row, columns)
                plan.settlements.append(settlement)
                _remember(plan, seen_names, settlement.from_name)
                _remember(plan, seen_names, settlement.to_name)
        except ValueError as exc:
            plan.errors.append("Row {}: {}".format(number, exc))

    if not saw_section:
        plan.errors.append(
            'This doesn\'t look like an exported CSV — no "Expenses" or "Settlements" section found.'
        )
    elif not plan.errors and not plan.expenses and not plan.settlements:
        plan.errors.append("There are no expenses or settlements in this file.")
    return plan


def _is_blank(row: list[str]) -> bool:
    return all(not cell.strip() for cell in row)


def _section_title(row: list[str]) -> str | None:
    """A section title is one cell on its own row, as the export writes it."""
    if not row:
        return None
    title = row[0].strip().casefold()
    if title in _SECTIONS and _is_blank(row[1:]):
        return title
    return None


def _cell(row: list[str], columns: dict[str, int], name: str) -> str:
    """This row's value for a column, by name. Blank for an optional column the
    file doesn't have, and for a row that ends early — a short row is a missing
    value, not a crash."""
    index = columns.get(name.casefold())
    if index is None or index >= len(row):
        return ""
    return row[index].strip()


def _remember(plan: ImportPlan, seen: dict[str, str], name: str) -> None:
    """Collect a referenced member name once, keeping its first spelling — the
    same casefold rule the roster uses, so "chris" and "Chris" in one file are
    one person, not two."""
    if name.casefold() not in seen:
        seen[name.casefold()] = name
        plan.member_names.append(name)


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError('Date must be YYYY-MM-DD (got "{}").'.format(raw))


def _member_name(raw: str, label: str) -> str:
    name = raw.strip()
    if not name:
        raise ValueError("{} is empty.".format(label))
    if len(name) > _MAX_NAME_LEN:
        raise ValueError("{} is too long (over {} characters).".format(label, _MAX_NAME_LEN))
    return name


def _parse_shares(raw: str) -> list[tuple[str, int]]:
    """"Chris: 33.34; Alex: 33.33" -> [("Chris", 3334), ("Alex", 3333)].

    Split on the last colon, not the first: an amount never contains one, but
    a member name might."""
    shares: list[tuple[str, int]] = []
    seen: set[str] = set()
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        name, separator, amount = part.rpartition(":")
        if not separator:
            raise ValueError(
                'Each participant must be written as "Name: amount" (got "{}").'.format(part)
            )
        name = _member_name(name, "A participant name")
        if name.casefold() in seen:
            raise ValueError('"{}" appears twice in Participants & Shares.'.format(name))
        seen.add(name.casefold())
        shares.append((name, parse_share_cents(amount)))
    if not shares:
        raise ValueError("Participants & Shares is empty.")
    return shares


def _expense_row(number: int, row: list[str], columns: dict[str, int]) -> ParsedExpense:
    when = _parse_date(_cell(row, columns, "Date"))

    description = _cell(row, columns, "Description")
    if not description:
        raise ValueError("Description is empty.")

    amount_cents = parse_amount_cents(_cell(row, columns, "Amount"))
    payer = _member_name(_cell(row, columns, "Payer"), "Payer")

    split_type = _cell(row, columns, "Split Type").casefold()
    if split_type not in ("equal", "exact"):
        raise ValueError('Split Type must be "equal" or "exact".')

    # Shares are stored, not recomputed (see CLAUDE.md): an equal split keeps
    # the exact cents the file carries rather than being re-split here, so a
    # re-import can't hand the rounding cent to a different member. That makes
    # the sum check apply to both split types.
    shares = _parse_shares(_cell(row, columns, "Participants & Shares"))
    try:
        validate_exact(amount_cents, {name.casefold(): cents for name, cents in shares})
    except ValueError as exc:
        raise ValueError("Participants & Shares don't add up to Amount ({}).".format(exc))

    # The second .strip() matters for the same reason it does on the expense
    # form: truncating can leave a trailing space, which would make an
    # otherwise-identical category miss its own tab.
    category = _cell(row, columns, "Category")[:_MAX_CATEGORY_LEN].strip() or None

    return ParsedExpense(
        row=number,
        date=when,
        description=description,
        category=category,
        amount_cents=amount_cents,
        payer=payer,
        split_type=split_type,
        shares=shares,
    )


def _settlement_row(number: int, row: list[str], columns: dict[str, int]) -> ParsedSettlement:
    when = _parse_date(_cell(row, columns, "Date"))
    from_name = _member_name(_cell(row, columns, "From"), "From")
    to_name = _member_name(_cell(row, columns, "To"), "To")
    if from_name.casefold() == to_name.casefold():
        raise ValueError("From and To must be different people.")
    return ParsedSettlement(
        row=number,
        date=when,
        from_name=from_name,
        to_name=to_name,
        amount_cents=parse_amount_cents(_cell(row, columns, "Amount")),
        note=_cell(row, columns, "Note") or None,
    )
