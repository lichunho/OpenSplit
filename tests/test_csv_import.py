"""
Parser tests. csv_import is DB-free like money.py, so these hit it directly —
no TestClient, no Session. The route-level round trip lives in test_routes.py.
"""
from datetime import date

from app.csv_import import parse_export_csv

# The shape the export writes: a one-cell section title, a header row, data
# rows, blank row between sections.
EXPORTED = '''Expenses
Date,Description,Category,Amount,Payer,Split Type,Participants & Shares
2026-08-14,"Dinner, ""fancy""",Food,100.00,Chris,equal,Chris: 33.34; Alex: 33.33; Sam: 33.33
2026-08-13,Taxi,,45.00,Alex,exact,Chris: 20.00; Alex: 15.00; Sam: 10.00

Settlements
Date,From,To,Amount,Note
2026-08-15,Sam,Chris,10.00,partial

Balances
Member,Balance
Chris,46.67
Alex,-3.34
Sam,-43.33
'''


def _expenses_section(header: str, *rows: str) -> str:
    return "Expenses\n" + header + "\n" + "\n".join(rows) + "\n"


def test_parses_a_full_export():
    plan = parse_export_csv(EXPORTED)

    assert plan.errors == []
    assert len(plan.expenses) == 2
    assert len(plan.settlements) == 1

    dinner, taxi = plan.expenses
    assert dinner.date == date(2026, 8, 14)
    assert dinner.description == 'Dinner, "fancy"'
    assert dinner.category == "Food"
    assert dinner.amount_cents == 10000
    assert dinner.payer == "Chris"
    assert dinner.split_type == "equal"
    assert dinner.shares == [("Chris", 3334), ("Alex", 3333), ("Sam", 3333)]

    assert taxi.category is None
    assert taxi.split_type == "exact"
    assert taxi.shares == [("Chris", 2000), ("Alex", 1500), ("Sam", 1000)]

    settlement = plan.settlements[0]
    assert (settlement.from_name, settlement.to_name) == ("Sam", "Chris")
    assert settlement.amount_cents == 1000
    assert settlement.note == "partial"


def test_columns_are_read_by_name_not_position():
    """The headline requirement: a future export may reorder columns or add
    new ones, and this must keep importing without a code change."""
    reordered = _expenses_section(
        "Payer,Amount,Currency,Split Type,Date,Participants & Shares,Description,Category,Receipt URL",
        "Chris,100.00,USD,equal,2026-08-14,Chris: 50.00; Alex: 50.00,Dinner,Food,https://example.test/r/1",
    )

    plan = parse_export_csv(reordered)

    assert plan.errors == []
    expense = plan.expenses[0]
    assert expense.description == "Dinner"
    assert expense.payer == "Chris"
    assert expense.amount_cents == 10000
    assert expense.category == "Food"
    assert expense.shares == [("Chris", 5000), ("Alex", 5000)]


def test_header_names_match_ignoring_case_and_padding():
    plan = parse_export_csv(
        _expenses_section(
            "  DATE ,Description,  amount,PAYER,split type,Participants & SHARES",
            "2026-08-14,Dinner,10.00,Chris,equal,Chris: 10.00",
        )
    )

    assert plan.errors == []
    assert plan.expenses[0].amount_cents == 1000


def test_missing_required_column_is_named_and_no_rows_import():
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Split Type,Participants & Shares",
            "2026-08-14,Dinner,10.00,equal,Chris: 10.00",
        )
    )

    assert plan.expenses == []
    assert len(plan.errors) == 1
    assert "Payer" in plan.errors[0]


def test_optional_columns_may_be_absent():
    """A file exported before Category and Note existed still imports."""
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner,10.00,Chris,equal,Chris: 10.00",
        )
        + "\nSettlements\nDate,From,To,Amount\n2026-08-15,Alex,Chris,5.00\n"
    )

    assert plan.errors == []
    assert plan.expenses[0].category is None
    assert plan.settlements[0].note is None


def test_equal_split_keeps_the_cents_from_the_file():
    """Shares are stored, not recomputed. Re-splitting a 3-way $100 here would
    be free to hand the rounding cent to a different member than the original
    expense did, silently changing two people's balances on a round trip."""
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner,100.00,Chris,equal,Chris: 33.33; Alex: 33.33; Sam: 33.34",
        )
    )

    assert plan.errors == []
    assert plan.expenses[0].shares == [("Chris", 3333), ("Alex", 3333), ("Sam", 3334)]


def test_shares_that_do_not_add_up_are_rejected_with_their_row():
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner,10.00,Chris,equal,Chris: 4.00; Alex: 5.00",
        )
    )

    assert plan.expenses == []
    assert plan.errors == ["Row 3: Participants & Shares don't add up to Amount (shares sum to 900 cents, expected 1000)."]


def test_every_bad_row_is_reported_and_good_rows_still_parse():
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-13-99,Bad date,10.00,Chris,equal,Chris: 10.00",
            "2026-08-14,,10.00,Chris,equal,Chris: 10.00",
            "2026-08-14,Scientific notation,1e5,Chris,equal,Chris: 10.00",
            "2026-08-14,Negative,-10.00,Chris,equal,Chris: -10.00",
            "2026-08-14,Bad split,10.00,Chris,percentage,Chris: 10.00",
            "2026-08-14,Duplicate name,10.00,Chris,exact,Chris: 5.00; chris: 5.00",
            "2026-08-14,Good,10.00,Chris,equal,Chris: 10.00",
        )
    )

    assert [e.description for e in plan.expenses] == ["Good"]
    assert len(plan.errors) == 6
    assert plan.errors[0].startswith("Row 3:")
    assert plan.errors[-1].startswith("Row 8:")
    assert "Description is empty" in plan.errors[1]
    assert "percentage" not in plan.errors[4] and "Split Type" in plan.errors[4]
    assert "appears twice" in plan.errors[5]


def test_member_names_are_collected_once_in_first_seen_order():
    plan = parse_export_csv(EXPORTED)

    assert plan.member_names == ["Chris", "Alex", "Sam"]


def test_names_differing_only_in_case_are_one_member():
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner,10.00,Chris,equal,chris: 5.00; Alex: 5.00",
        )
    )

    assert plan.errors == []
    assert plan.member_names == ["Chris", "Alex"]


def test_balances_section_is_ignored():
    """Balances is derived from the other two sections — importing it would
    double-count every expense in the file."""
    plan = parse_export_csv(EXPORTED)

    assert len(plan.expenses) == 2
    assert len(plan.settlements) == 1
    assert plan.errors == []


def test_a_file_with_only_expenses_parses():
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner,10.00,Chris,equal,Chris: 10.00",
        )
    )

    assert plan.errors == []
    assert len(plan.expenses) == 1
    assert plan.settlements == []


def test_a_settlement_cannot_be_between_one_person():
    plan = parse_export_csv("Settlements\nDate,From,To,Amount\n2026-08-15,Chris,Chris,5.00\n")

    assert plan.settlements == []
    assert "different people" in plan.errors[0]


def test_a_short_row_is_a_missing_value_not_a_crash():
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner",
        )
    )

    assert plan.expenses == []
    assert plan.errors[0].startswith("Row 3:")


def test_a_file_that_is_not_an_export_says_so():
    plan = parse_export_csv("name,total\nDinner,100\n")

    assert plan.expenses == []
    assert len(plan.errors) == 1
    assert "doesn't look like an exported CSV" in plan.errors[0]


def test_an_empty_export_has_nothing_to_import():
    plan = parse_export_csv(
        "Expenses\nDate,Description,Amount,Payer,Split Type,Participants & Shares\n"
    )

    assert plan.errors == ["There are no expenses or settlements in this file."]


def test_a_member_name_containing_a_colon_survives():
    """Shares split on the *last* colon: an amount never contains one, a name
    might."""
    plan = parse_export_csv(
        _expenses_section(
            "Date,Description,Amount,Payer,Split Type,Participants & Shares",
            "2026-08-14,Dinner,10.00,DJ: Sam,equal,DJ: Sam: 10.00",
        )
    )

    assert plan.errors == []
    assert plan.expenses[0].shares == [("DJ: Sam", 1000)]
    assert plan.member_names == ["DJ: Sam"]
