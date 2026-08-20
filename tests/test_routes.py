"""
Happy-path route flows plus the auth edges that matter: a wrong password
must never 500 (trap #2), duplicate names are rejected case-insensitively,
an unidentified visitor is bounced to identify, and switch clears only the
group it's called on.
"""
import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine
from app.models import Expense, Group, Member, Settlement, Share


def _create_group(client: TestClient, name: str = "Trip") -> str:
    """Returns the new group's slug."""
    response = client.post("/groups", data={"name": name}, follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/g/")
    return location.removeprefix("/g/")


def test_create_group_redirects_to_group_page(client: TestClient):
    slug = _create_group(client)
    response = client.get(f"/g/{slug}", follow_redirects=False)
    # No identity yet in this fresh client -> bounced to identify.
    assert response.status_code == 303
    assert response.headers["location"] == f"/g/{slug}/identify"


def test_identify_as_new_member_then_view_group(client: TestClient):
    slug = _create_group(client)
    response = client.post(
        f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/g/{slug}"

    page = client.get(f"/g/{slug}")
    assert page.status_code == 200
    assert "Chris" in page.text


def test_new_member_can_set_a_password_immediately(client: TestClient):
    slug = _create_group(client)
    client.post(
        f"/g/{slug}/identify",
        data={"new_name": "Chris", "password": "hunter2"},
        follow_redirects=False,
    )

    # A different browser (fresh cookie jar) must be asked who they are.
    other_browser = TestClient(client.app)
    identify_page = other_browser.get(f"/g/{slug}/identify")
    assert "Chris" in identify_page.text


def test_wrong_password_rerenders_form_without_500(client: TestClient):
    slug = _create_group(client)
    client.post(
        f"/g/{slug}/identify",
        data={"new_name": "Chris", "password": "hunter2"},
        follow_redirects=False,
    )
    members_page = client.get(f"/g/{slug}/identify")
    # Pull Chris's member id out of the rendered <option value="..."> so the
    # test doesn't need direct DB access.
    import re

    match = re.search(r'<option value="(\d+)">Chris', members_page.text)
    assert match, "Chris should appear as a selectable existing member"
    chris_id = match.group(1)

    other_browser = TestClient(client.app)
    response = other_browser.post(
        f"/g/{slug}/identify",
        data={"existing_member_id": chris_id, "password": "wrong-password"},
    )
    assert response.status_code != 500
    assert response.status_code == 400
    assert "wrong" in response.text.lower() or "error" in response.text.lower()

    # And the correct password succeeds for that same "different member".
    correct = other_browser.post(
        f"/g/{slug}/identify",
        data={"existing_member_id": chris_id, "password": "hunter2"},
        follow_redirects=False,
    )
    assert correct.status_code == 303


def test_identify_as_a_different_member_succeeds(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    other_browser = TestClient(client.app)
    response = other_browser.post(
        f"/g/{slug}/identify", data={"new_name": "Alex"}, follow_redirects=False
    )
    assert response.status_code == 303
    page = other_browser.get(f"/g/{slug}")
    assert "Alex" in page.text


def test_case_differing_duplicate_name_is_rejected(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    other_browser = TestClient(client.app)
    response = other_browser.post(f"/g/{slug}/identify", data={"new_name": "chris"})
    assert response.status_code != 500
    assert response.status_code == 400
    assert "taken" in response.text.lower()


def test_malformed_member_id_renders_an_error_not_a_500(client: TestClient):
    # The identify form is public, so a mangled form value must degrade to the
    # error page rather than raising out of int().
    slug = _create_group(client)
    response = client.post(f"/g/{slug}/identify", data={"existing_member_id": "not-a-number"})
    assert response.status_code == 400
    assert "no longer exists" in response.text.lower()


def test_add_member_rejects_a_case_and_space_differing_duplicate(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)
    client.post(f"/g/{slug}/members", data={"name": "Sam"}, follow_redirects=False)
    client.post(f"/g/{slug}/members", data={"name": "  sam "}, follow_redirects=False)

    # Assert the stored rows, not the rendered HTML — the page wording is free
    # to change, the "one Sam per group" invariant is not.
    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        names = [m.name for m in session.exec(select(Member).where(Member.group_id == group.id)).all()]
    assert sorted(names) == ["Chris", "Sam"]


def test_group_page_without_identity_redirects_to_identify(client: TestClient):
    slug = _create_group(client)
    fresh = TestClient(client.app)
    response = fresh.get(f"/g/{slug}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/g/{slug}/identify"


def test_switch_clears_only_that_groups_identity(client: TestClient):
    slug_a = _create_group(client, "Group A")
    slug_b = _create_group(client, "Group B")
    client.post(f"/g/{slug_a}/identify", data={"new_name": "Chris"}, follow_redirects=False)
    client.post(f"/g/{slug_b}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    switch_response = client.post(f"/g/{slug_a}/switch", follow_redirects=False)
    assert switch_response.status_code == 303
    assert switch_response.headers["location"] == f"/g/{slug_a}/identify"

    # Group A identity is gone...
    a_page = client.get(f"/g/{slug_a}", follow_redirects=False)
    assert a_page.status_code == 303
    assert a_page.headers["location"] == f"/g/{slug_a}/identify"

    # ...but group B identity is untouched.
    b_page = client.get(f"/g/{slug_b}", follow_redirects=False)
    assert b_page.status_code == 200


def test_add_member_route(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)
    client.post(f"/g/{slug}/members", data={"name": "Sam"}, follow_redirects=False)

    page = client.get(f"/g/{slug}")
    assert "Sam" in page.text


def test_robots_txt_disallows_everything(client: TestClient):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert "Disallow: /" in response.text


def _member_id(slug: str, name: str) -> int:
    """Query the test DB directly for a member's id, rather than scraping it
    out of rendered HTML — expense tests need real ids for form fields."""
    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        member = session.exec(
            select(Member).where(Member.group_id == group.id, Member.name == name)
        ).first()
        return member.id


def _create_trio(client: TestClient, slug: str) -> tuple[int, int, int]:
    """Identify as Chris, add Alex and Sam, return their member ids in that
    creation order (which is also ascending id order)."""
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)
    client.post(f"/g/{slug}/members", data={"name": "Alex"}, follow_redirects=False)
    client.post(f"/g/{slug}/members", data={"name": "Sam"}, follow_redirects=False)
    return _member_id(slug, "Chris"), _member_id(slug, "Alex"), _member_id(slug, "Sam")


def test_equal_split_three_ways_stores_expected_shares(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Dinner",
            "amount": "100.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expense = session.exec(select(Expense).where(Expense.group_id == group.id)).first()
        assert expense.amount_cents == 10000
        shares = session.exec(select(Share).where(Share.expense_id == expense.id)).all()

    shares_by_member = {s.member_id: s.amount_cents for s in shares}
    assert set(shares_by_member) == {chris_id, alex_id, sam_id}
    assert sorted(shares_by_member.values()) == [3333, 3333, 3334]
    assert sum(shares_by_member.values()) == 10000


def test_exact_split_accepted_when_shares_sum_to_total(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Taxi",
            "amount": "45.00",
            "payer_id": str(alex_id),
            "split_type": "exact",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
            f"share_{chris_id}": "20.00",
            f"share_{alex_id}": "15.00",
            f"share_{sam_id}": "10.00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expense = session.exec(
            select(Expense).where(Expense.group_id == group.id, Expense.description == "Taxi")
        ).first()
        shares = session.exec(select(Share).where(Share.expense_id == expense.id)).all()
    shares_by_member = {s.member_id: s.amount_cents for s in shares}
    assert shares_by_member == {chris_id: 2000, alex_id: 1500, sam_id: 1000}


def test_exact_split_rejected_when_shares_dont_sum_to_total(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Taxi",
            "amount": "45.00",
            "payer_id": str(alex_id),
            "split_type": "exact",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
            f"share_{chris_id}": "20.00",
            f"share_{alex_id}": "15.00",
            f"share_{sam_id}": "9.00",
        },
    )
    assert response.status_code != 500
    assert response.status_code == 400
    assert "sum" in response.text.lower() or "error" in response.text.lower()

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expense = session.exec(
            select(Expense).where(Expense.group_id == group.id, Expense.description == "Taxi")
        ).first()
    assert expense is None


def test_non_numeric_amount_is_rejected_without_500(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Snacks",
            "amount": "abc",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id)],
        },
    )
    assert response.status_code != 500
    assert response.status_code == 400
    assert "amount" in response.text.lower()


def test_zero_and_negative_amounts_are_rejected(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    for bad_amount in ("0", "-5.00"):
        response = client.post(
            f"/g/{slug}/expenses/new",
            data={
                "description": "Snacks",
                "amount": bad_amount,
                "payer_id": str(chris_id),
                "split_type": "equal",
                "participant_ids": [str(chris_id)],
            },
        )
        assert response.status_code != 500
        assert response.status_code == 400
        assert "greater than zero" in response.text.lower()


def test_decimal_only_looking_amounts_are_rejected(client: TestClient):
    # Decimal() accepts all of these, so without a strict gate "1e5" would
    # book a $100,000 expense and "NaN" would slip past the `<= 0` check.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    for bad_amount in ("1e5", "NaN", "Infinity", "1,234.56", "$10"):
        response = client.post(
            f"/g/{slug}/expenses/new",
            data={
                "description": "Snacks",
                "amount": bad_amount,
                "payer_id": str(chris_id),
                "split_type": "equal",
                "participant_ids": [str(chris_id)],
            },
        )
        assert response.status_code == 400, f"{bad_amount} should be rejected"
        assert "valid amount" in response.text.lower()

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        assert session.exec(select(Expense).where(Expense.group_id == group.id)).all() == []


def test_duplicate_participant_is_rejected_for_both_split_types(client: TestClient):
    # An exact split builds a {member_id: cents} dict, which would silently
    # collapse a repeated id and store fewer shares than were submitted.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    for split_type, extra in (("equal", {}), ("exact", {f"share_{chris_id}": "20.00"})):
        response = client.post(
            f"/g/{slug}/expenses/new",
            data={
                "description": "Snacks",
                "amount": "20.00",
                "payer_id": str(chris_id),
                "split_type": split_type,
                "participant_ids": [str(chris_id), str(chris_id)],
                **extra,
            },
        )
        assert response.status_code == 400, f"{split_type} should reject a duplicate"
        assert "duplicate" in response.text.lower()


def test_soft_delete_and_restore_expense(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Snacks",
            "amount": "12.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id)],
        },
        follow_redirects=False,
    )

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expense = session.exec(select(Expense).where(Expense.group_id == group.id)).first()
        expense_id = expense.id

    delete_response = client.post(f"/g/{slug}/expenses/{expense_id}/delete", follow_redirects=False)
    assert delete_response.status_code == 303
    with Session(engine) as session:
        expense = session.get(Expense, expense_id)
        assert expense is not None  # soft delete never removes the row
        assert expense.deleted_at is not None

    restore_response = client.post(f"/g/{slug}/expenses/{expense_id}/restore", follow_redirects=False)
    assert restore_response.status_code == 303
    with Session(engine) as session:
        expense = session.get(Expense, expense_id)
        assert expense.deleted_at is None


def test_payer_from_a_different_group_is_rejected(client: TestClient):
    slug_a = _create_group(client, "Group A")
    slug_b = _create_group(client, "Group B")
    client.post(f"/g/{slug_a}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    other_browser = TestClient(client.app)
    other_browser.post(f"/g/{slug_b}/identify", data={"new_name": "Intruder"}, follow_redirects=False)
    intruder_id = _member_id(slug_b, "Intruder")

    response = client.post(
        f"/g/{slug_a}/expenses/new",
        data={
            "description": "Sneaky",
            "amount": "10.00",
            "payer_id": str(intruder_id),
            "split_type": "equal",
            "participant_ids": [str(intruder_id)],
        },
    )
    assert response.status_code != 500
    assert response.status_code == 400

    with Session(engine) as session:
        group_a = session.exec(select(Group).where(Group.slug == slug_a)).first()
        expense = session.exec(
            select(Expense).where(Expense.group_id == group_a.id, Expense.description == "Sneaky")
        ).first()
    assert expense is None


def _only_expense_id(slug: str) -> int:
    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        return session.exec(select(Expense).where(Expense.group_id == group.id)).first().id


def _shares_by_member(expense_id: int) -> dict[int, int]:
    with Session(engine) as session:
        rows = session.exec(select(Share).where(Share.expense_id == expense_id)).all()
    # Built from a list, not a comprehension over the query, so a duplicated
    # share row (the failure mode an edit could introduce) shows up as a length
    # mismatch instead of being silently collapsed by the dict.
    assert len(rows) == len({r.member_id for r in rows})
    return {r.member_id: r.amount_cents for r in rows}


def test_edit_expense_updates_amount_and_replaces_shares(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id, sam_id])
    expense_id = _only_expense_id(slug)

    response = client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={
            "description": "Dinner",
            "amount": "60.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(engine) as session:
        expense = session.get(Expense, expense_id)
        assert expense.amount_cents == 6000
        assert expense.description == "Dinner"

    assert _shares_by_member(expense_id) == {chris_id: 2000, alex_id: 2000, sam_id: 2000}


def test_edit_keeps_the_expenses_place_in_the_feed(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id])
    expense_id = _only_expense_id(slug)
    with Session(engine) as session:
        created_at = session.get(Expense, expense_id).created_at

    client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={
            "description": "Dinner and drinks",
            "amount": "40.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id)],
        },
        follow_redirects=False,
    )

    with Session(engine) as session:
        # created_at drives the activity feed's ordering, so an edit that
        # touched it would jump the expense to the top of the list.
        assert session.get(Expense, expense_id).created_at == created_at


def test_edit_changing_payer_moves_the_balance(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id, sam_id])
    expense_id = _only_expense_id(slug)
    assert _balances(slug) == {chris_id: 2000, alex_id: -1000, sam_id: -1000}

    client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={
            "description": "Dinner",
            "amount": "30.00",
            "payer_id": str(alex_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
        },
        follow_redirects=False,
    )

    assert _balances(slug) == {chris_id: -1000, alex_id: 2000, sam_id: -1000}


def test_edit_from_equal_to_exact_replaces_shares(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id, sam_id])
    expense_id = _only_expense_id(slug)

    response = client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={
            "description": "Dinner",
            "amount": "30.00",
            "payer_id": str(chris_id),
            "split_type": "exact",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
            f"share_{chris_id}": "15.00",
            f"share_{alex_id}": "10.00",
            f"share_{sam_id}": "5.00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(engine) as session:
        assert session.get(Expense, expense_id).split_type == "exact"
    assert _shares_by_member(expense_id) == {chris_id: 1500, alex_id: 1000, sam_id: 500}


def test_edit_dropping_a_participant_removes_their_share(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id, sam_id])
    expense_id = _only_expense_id(slug)

    client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={
            "description": "Dinner",
            "amount": "30.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id)],
        },
        follow_redirects=False,
    )

    # Sam's row must be gone, not left behind at its old amount.
    assert _shares_by_member(expense_id) == {chris_id: 1500, alex_id: 1500}
    assert _balances(slug) == {chris_id: 1500, alex_id: -1500}


def test_edit_invalid_amount_rerenders_400_and_leaves_expense_unchanged(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id, sam_id])
    expense_id = _only_expense_id(slug)

    response = client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={
            "description": "Dinner",
            "amount": "abc",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id), str(sam_id)],
        },
    )
    assert response.status_code == 400
    assert "Enter a valid amount." in response.text
    # The form comes back still filled in, posting to the edit route.
    assert f"/g/{slug}/expenses/{expense_id}/edit" in response.text
    assert 'value="abc"' in response.text

    with Session(engine) as session:
        assert session.get(Expense, expense_id).amount_cents == 3000
    assert _shares_by_member(expense_id) == {chris_id: 1000, alex_id: 1000, sam_id: 1000}


def test_edit_expense_from_another_group_is_rejected(client: TestClient):
    slug_a = _create_group(client, "Group A")
    slug_b = _create_group(client, "Group B")
    chris_id, alex_id, _sam_id = _create_trio(client, slug_a)
    _add_expense(client, slug_a, "Dinner", "30.00", chris_id, [chris_id, alex_id])
    expense_id = _only_expense_id(slug_a)

    other_browser = TestClient(client.app)
    other_browser.post(f"/g/{slug_b}/identify", data={"new_name": "Intruder"}, follow_redirects=False)
    intruder_id = _member_id(slug_b, "Intruder")

    response = other_browser.post(
        f"/g/{slug_b}/expenses/{expense_id}/edit",
        data={
            "description": "Hijacked",
            "amount": "999.00",
            "payer_id": str(intruder_id),
            "split_type": "equal",
            "participant_ids": [str(intruder_id)],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303

    with Session(engine) as session:
        expense = session.get(Expense, expense_id)
        assert expense.description == "Dinner"
        assert expense.amount_cents == 3000


def test_edit_of_a_deleted_expense_is_refused_until_it_is_restored(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id])
    expense_id = _only_expense_id(slug)
    client.post(f"/g/{slug}/expenses/{expense_id}/delete", follow_redirects=False)

    edit_data = {
        "description": "Resurrected",
        "amount": "99.00",
        "payer_id": str(chris_id),
        "split_type": "equal",
        "participant_ids": [str(chris_id), str(alex_id)],
    }
    assert client.get(f"/g/{slug}/expenses/{expense_id}/edit", follow_redirects=False).status_code == 303
    assert client.post(
        f"/g/{slug}/expenses/{expense_id}/edit", data=edit_data, follow_redirects=False
    ).status_code == 303

    with Session(engine) as session:
        expense = session.get(Expense, expense_id)
        assert expense.description == "Dinner"
        assert expense.deleted_at is not None

    # Restore first, and the same edit goes through.
    client.post(f"/g/{slug}/expenses/{expense_id}/restore", follow_redirects=False)
    assert client.post(
        f"/g/{slug}/expenses/{expense_id}/edit", data=edit_data, follow_redirects=False
    ).status_code == 303
    with Session(engine) as session:
        assert session.get(Expense, expense_id).description == "Resurrected"


def test_edit_form_prefills_the_stored_values(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    # Sam is deliberately left out, so his checkbox must come back unchecked.
    _add_expense(client, slug, "Dinner", "20.00", alex_id, [chris_id, alex_id])
    expense_id = _only_expense_id(slug)

    response = client.get(f"/g/{slug}/expenses/{expense_id}/edit")
    assert response.status_code == 200
    assert 'value="Dinner"' in response.text
    assert 'value="20.00"' in response.text
    assert f'<option value="{alex_id}" selected>' in response.text

    checked = {
        member_id: "checked" in tag
        for member_id, tag in re.findall(
            r'<input type="checkbox" name="participant_ids" value="(\d+)"(.*?)>',
            response.text,
            re.DOTALL,
        )
    }
    assert checked == {str(chris_id): True, str(alex_id): True, str(sam_id): False}


def _add_expense(
    client: TestClient,
    slug: str,
    description: str,
    amount: str,
    payer_id: int,
    participant_ids: list[int],
    split_type: str = "equal",
    shares: dict[int, str] | None = None,
    category: str | None = None,
    custom_category: str | None = None,
) -> None:
    data = {
        "description": description,
        "amount": amount,
        "payer_id": str(payer_id),
        "split_type": split_type,
        "participant_ids": [str(pid) for pid in participant_ids],
    }
    if category is not None:
        data["category"] = category
    if custom_category is not None:
        data["custom_category"] = custom_category
    for member_id, share_amount in (shares or {}).items():
        data[f"share_{member_id}"] = share_amount
    response = client.post(f"/g/{slug}/expenses/new", data=data, follow_redirects=False)
    assert response.status_code == 303


def test_dashboard_balances_sum_to_zero_and_suggestion_is_stable_across_reloads(client: TestClient):
    # Plan verification step 6: Chris pays $100 dinner split 3 ways, Alex pays
    # $45 taxi exact 20/15/10.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "100.00", chris_id, [chris_id, alex_id, sam_id])
    _add_expense(
        client,
        slug,
        "Taxi",
        "45.00",
        alex_id,
        [chris_id, alex_id, sam_id],
        split_type="exact",
        shares={chris_id: "20.00", alex_id: "15.00", sam_id: "10.00"},
    )

    first = client.get(f"/g/{slug}")
    second = client.get(f"/g/{slug}")
    assert first.status_code == 200 and second.status_code == 200

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expenses = session.exec(select(Expense).where(Expense.group_id == group.id)).all()
        shares = []
        for e in expenses:
            shares.extend(session.exec(select(Share).where(Share.expense_id == e.id)).all())

    from app.money import net_balances, simplify

    balances = net_balances(
        [(e.payer_id, e.amount_cents) for e in expenses],
        [(s.member_id, s.amount_cents) for s in shares],
        [],
    )
    assert sum(balances.values()) == 0

    transfers = simplify(balances)
    assert len(transfers) <= 2  # n - 1 for 3 members

    # Reloading twice must yield an identical suggestion (deterministic tie-break).
    import re

    def suggestions(text: str) -> list[str]:
        # Bounded at </section> so this compares the suggestions only, and
        # can't silently pass or fail on the Members/Activity lists below it.
        section = text.split("<h2>Suggested settlements</h2>", 1)[1].split("</section>", 1)[0]
        return re.findall(r"<li>(.*?)</li>", section)

    assert suggestions(first.text) == suggestions(second.text)
    assert suggestions(first.text) != []  # sanity: there is something to compare


def test_soft_delete_changes_balances_and_restore_returns_them_exactly(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "100.00", chris_id, [chris_id, alex_id, sam_id])
    _add_expense(
        client,
        slug,
        "Taxi",
        "45.00",
        alex_id,
        [chris_id, alex_id, sam_id],
        split_type="exact",
        shares={chris_id: "20.00", alex_id: "15.00", sam_id: "10.00"},
    )
    # Dinner's equal 3-way split of $100 rotates its odd cent onto the second
    # member by id (Alex): 3333 / 3334 / 3333 for chris / alex / sam.
    # Combined with the exact taxi shares (2000/1500/1000):
    #   chris: paid 10000, owes 3333+2000=5333 -> balance  4667
    #   alex:  paid  4500, owes 3334+1500=4834 -> balance  -334
    #   sam:   paid     0, owes 3333+1000=4333 -> balance -4333

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        taxi = session.exec(
            select(Expense).where(Expense.group_id == group.id, Expense.description == "Taxi")
        ).first()
        taxi_id = taxi.id

    from app.money import net_balances

    def balances_now() -> dict[int, int]:
        with Session(engine) as session:
            group = session.exec(select(Group).where(Group.slug == slug)).first()
            expenses = session.exec(
                select(Expense).where(Expense.group_id == group.id, Expense.deleted_at.is_(None))
            ).all()
            shares = []
            for e in expenses:
                shares.extend(session.exec(select(Share).where(Share.expense_id == e.id)).all())
        return net_balances(
            [(e.payer_id, e.amount_cents) for e in expenses],
            [(s.member_id, s.amount_cents) for s in shares],
            [],
        )

    before = balances_now()
    assert before == {chris_id: 4667, alex_id: -334, sam_id: -4333}

    # The live dashboard must show the same figures the DB does.
    page = client.get(f"/g/{slug}")
    assert "$46.67" in page.text  # chris (me) is owed 4667 cents

    client.post(f"/g/{slug}/expenses/{taxi_id}/delete", follow_redirects=False)
    after_delete = balances_now()
    assert after_delete == {chris_id: 6667, alex_id: -3334, sam_id: -3333}

    client.post(f"/g/{slug}/expenses/{taxi_id}/restore", follow_redirects=False)
    after_restore = balances_now()
    assert after_restore == before


def test_soft_deleted_expenses_shares_are_excluded_from_balances(client: TestClient):
    # The subtle trap: Share rows carry no deleted_at of their own. A query
    # that filters Expense.deleted_at IS NULL but sums Share rows without
    # joining back through Expense would still charge everyone their share of
    # a deleted expense while its paid-amount side correctly disappears —
    # producing balances that don't sum to zero and never read "settled up".
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Snacks", "30.00", chris_id, [chris_id, alex_id, sam_id])

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expense = session.exec(select(Expense).where(Expense.group_id == group.id)).first()
        expense_id = expense.id

    client.post(f"/g/{slug}/expenses/{expense_id}/delete", follow_redirects=False)

    # Correct behaviour, exercised through the real route: everyone reads
    # settled up, and no leftover "owe"/"owed" copy remains in that section.
    page = client.get(f"/g/{slug}")
    assert page.status_code == 200
    balances_section = page.text.split("<h2>Balances</h2>", 1)[1].split("<h2>Suggested settlements</h2>", 1)[0]
    assert balances_section.lower().count("settled up") == 3
    assert "owe" not in balances_section.lower()

    with Session(engine) as session:
        # Soft delete never deletes rows: the Share rows are still there.
        all_shares = session.exec(select(Share).where(Share.expense_id == expense_id)).all()
    assert len(all_shares) == 3

    from app.money import net_balances

    # What a query that forgot the Share -> Expense join would produce: a
    # visibly wrong, non-zero-summing set of balances (everyone in debt, no
    # one owed, because the paid side vanished but the owed side didn't).
    naive_balances = net_balances([], [(s.member_id, s.amount_cents) for s in all_shares], [])
    assert sum(naive_balances.values()) != 0
    assert all(cents < 0 for cents in naive_balances.values())


def test_dashboard_renders_for_a_group_with_no_expenses(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    page = client.get(f"/g/{slug}")
    assert page.status_code == 200
    assert "settled up" in page.text.lower()
    assert "no activity yet" in page.text.lower()


# ---------------------------------------------------------------------------
# Settle-up flow. Every scenario below shares the same setup as
# test_soft_delete_changes_balances_and_restore_returns_them_exactly:
#   chris: paid 10000, owes 5333 -> balance  4667
#   alex:  paid  4500, owes 4834 -> balance  -334
#   sam:   paid     0, owes 4333 -> balance -4333
# simplify() on those balances pays the largest debtor first: sam -> chris
# 4333, then alex -> chris 334.
# ---------------------------------------------------------------------------


def _setup_dinner_and_taxi(client: TestClient, slug: str) -> tuple[int, int, int]:
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "100.00", chris_id, [chris_id, alex_id, sam_id])
    _add_expense(
        client,
        slug,
        "Taxi",
        "45.00",
        alex_id,
        [chris_id, alex_id, sam_id],
        split_type="exact",
        shares={chris_id: "20.00", alex_id: "15.00", sam_id: "10.00"},
    )
    return chris_id, alex_id, sam_id


def _balances(slug: str) -> dict[int, int]:
    from app.money import net_balances

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expenses = session.exec(
            select(Expense).where(Expense.group_id == group.id, Expense.deleted_at.is_(None))
        ).all()
        shares = []
        for e in expenses:
            shares.extend(session.exec(select(Share).where(Share.expense_id == e.id)).all())
        settlements = session.exec(
            select(Settlement).where(Settlement.group_id == group.id, Settlement.deleted_at.is_(None))
        ).all()
    return net_balances(
        [(e.payer_id, e.amount_cents) for e in expenses],
        [(s.member_id, s.amount_cents) for s in shares],
        [(s.from_member_id, s.to_member_id, s.amount_cents) for s in settlements],
    )


def _identify_as(client: TestClient, slug: str, member_id: int) -> TestClient:
    browser = TestClient(client.app)
    browser.post(f"/g/{slug}/identify", data={"existing_member_id": str(member_id)}, follow_redirects=False)
    return browser


def test_settle_as_sam_accepting_default_zeroes_balance_and_drops_suggestion(client: TestClient):
    # Plan verification step 7.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _setup_dinner_and_taxi(client, slug)
    sam_browser = _identify_as(client, slug, sam_id)

    confirm = sam_browser.get(f"/g/{slug}/settle/{chris_id}")
    assert confirm.status_code == 200
    assert 'value="43.33"' in confirm.text  # pre-filled with the suggested figure

    post = sam_browser.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(sam_id), "amount": "43.33"},
        follow_redirects=False,
    )
    assert post.status_code == 303

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        settlement = session.exec(select(Settlement).where(Settlement.group_id == group.id)).first()
    assert (settlement.from_member_id, settlement.to_member_id, settlement.amount_cents) == (sam_id, chris_id, 4333)

    balances = _balances(slug)
    assert balances[sam_id] == 0

    from app.money import simplify

    transfers = simplify(balances)
    assert all(debtor_id != sam_id for debtor_id, _creditor_id, _amt in transfers)


def test_settle_edited_downward_then_overpay_flips_sign(client: TestClient):
    # Plan verification step 8.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _setup_dinner_and_taxi(client, slug)
    alex_browser = _identify_as(client, slug, alex_id)

    confirm = alex_browser.get(f"/g/{slug}/settle/{chris_id}")
    assert 'value="3.34"' in confirm.text  # Alex's suggested figure is 334 cents

    downward = alex_browser.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(alex_id), "amount": "2.00"},
        follow_redirects=False,
    )
    assert downward.status_code == 303

    balances = _balances(slug)
    # alex still owes 334 - 200 = 134 cents; chris's credit drops by the same 200.
    assert balances == {chris_id: 4467, alex_id: -134, sam_id: -4333}
    assert sum(balances.values()) == 0

    sam_browser = _identify_as(client, slug, sam_id)
    overpay = sam_browser.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(sam_id), "amount": "50.00"},
        follow_redirects=False,
    )
    assert overpay.status_code == 303

    balances_after_overpay = _balances(slug)
    # sam owed 4333, paid 5000 -> the balance flips sign in sam's favour (667).
    assert balances_after_overpay[sam_id] == 667
    assert sum(balances_after_overpay.values()) == 0


def test_delete_settlement_restores_prior_balances_exactly(client: TestClient):
    # Plan verification step 9.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _setup_dinner_and_taxi(client, slug)
    before = _balances(slug)
    assert before == {chris_id: 4667, alex_id: -334, sam_id: -4333}

    sam_browser = _identify_as(client, slug, sam_id)
    sam_browser.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(sam_id), "amount": "43.33"},
        follow_redirects=False,
    )
    after_settle = _balances(slug)
    assert after_settle[sam_id] == 0

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        settlement = session.exec(select(Settlement).where(Settlement.group_id == group.id)).first()
        settlement_id = settlement.id

    delete_response = client.post(f"/g/{slug}/settlements/{settlement_id}/delete", follow_redirects=False)
    assert delete_response.status_code == 303
    with Session(engine) as session:
        settlement = session.get(Settlement, settlement_id)
        assert settlement is not None  # soft delete never removes the row
        assert settlement.deleted_at is not None
    assert _balances(slug) == before

    restore_response = client.post(f"/g/{slug}/settlements/{settlement_id}/restore", follow_redirects=False)
    assert restore_response.status_code == 303
    with Session(engine) as session:
        settlement = session.get(Settlement, settlement_id)
        assert settlement.deleted_at is None
    assert _balances(slug) == after_settle


def test_settle_amount_validation_rejected_without_500(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    for bad_amount in ("0", "-5.00", "abc", "1e5", "NaN", "Infinity"):
        response = client.post(
            f"/g/{slug}/settle",
            data={"to_member_id": str(alex_id), "from_member_id": str(chris_id), "amount": bad_amount},
        )
        assert response.status_code != 500, f"{bad_amount} should not 500"
        assert response.status_code == 400

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        assert session.exec(select(Settlement).where(Settlement.group_id == group.id)).all() == []


def test_settle_payer_equals_payee_is_rejected(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(chris_id), "amount": "5.00"},
    )
    assert response.status_code != 500
    assert response.status_code == 400
    assert "different" in response.text.lower() or "error" in response.text.lower()

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        assert session.exec(select(Settlement).where(Settlement.group_id == group.id)).all() == []


def test_settle_member_from_another_group_is_rejected(client: TestClient):
    slug_a = _create_group(client, "Group A")
    slug_b = _create_group(client, "Group B")
    chris_id, alex_id, sam_id = _create_trio(client, slug_a)

    other_browser = TestClient(client.app)
    other_browser.post(f"/g/{slug_b}/identify", data={"new_name": "Intruder"}, follow_redirects=False)
    intruder_id = _member_id(slug_b, "Intruder")

    response = client.post(
        f"/g/{slug_a}/settle",
        data={"to_member_id": str(intruder_id), "from_member_id": str(chris_id), "amount": "5.00"},
    )
    assert response.status_code != 500
    assert response.status_code == 400

    with Session(engine) as session:
        group_a = session.exec(select(Group).where(Group.slug == slug_a)).first()
        assert session.exec(select(Settlement).where(Settlement.group_id == group_a.id)).all() == []


def test_cross_group_settlement_delete_does_not_work(client: TestClient):
    slug_a = _create_group(client, "Group A")
    slug_b = _create_group(client, "Group B")
    chris_id, alex_id, sam_id = _create_trio(client, slug_a)

    client.post(
        f"/g/{slug_a}/settle",
        data={"to_member_id": str(alex_id), "from_member_id": str(chris_id), "amount": "5.00"},
        follow_redirects=False,
    )
    with Session(engine) as session:
        group_a = session.exec(select(Group).where(Group.slug == slug_a)).first()
        settlement = session.exec(select(Settlement).where(Settlement.group_id == group_a.id)).first()
        settlement_id = settlement.id

    other_browser = TestClient(client.app)
    other_browser.post(f"/g/{slug_b}/identify", data={"new_name": "Intruder"}, follow_redirects=False)
    cross_delete = other_browser.post(f"/g/{slug_b}/settlements/{settlement_id}/delete", follow_redirects=False)
    assert cross_delete.status_code == 303  # scoped no-op, not an error

    with Session(engine) as session:
        settlement = session.get(Settlement, settlement_id)
        assert settlement.deleted_at is None  # untouched by the other group's attempt


def test_settle_redirects_with_a_flash_message(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(alex_id), "from_member_id": str(chris_id), "amount": "12.00"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/g/{slug}?flash=")

    page = client.get(location)
    assert "you paid Alex $12.00" in page.text


def test_settle_pick_lists_all_other_members_not_just_suggestions(client: TestClient):
    # Plan is explicit: the "someone else" list must be visible, not collapsed.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _setup_dinner_and_taxi(client, slug)

    # Chris is owed by everyone, so simplify() suggests no transfer *from*
    # Chris — but Sam and Alex must still be pickable directly.
    page = client.get(f"/g/{slug}/settle")
    assert page.status_code == 200
    assert "Alex" in page.text
    assert "Sam" in page.text


# ---------------------------------------------------------------------------
# CSV export.
# ---------------------------------------------------------------------------


def test_csv_export_returns_200_with_correct_content_type(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "100.00", chris_id, [chris_id, alex_id, sam_id])

    response = client.get(f"/g/{slug}/export.csv")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert "attachment" in response.headers.get("content-disposition", "").lower()
    assert slug in response.headers.get("content-disposition", "")


def test_csv_export_balances_reconcile_with_dashboard(client: TestClient):
    # Plan verification step 10: Chris pays $100 dinner equal 3 ways; Alex pays
    # $45 taxi exact 20/15/10 → Chris +46.67, Alex −3.34, Sam −43.33
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _setup_dinner_and_taxi(client, slug)

    response = client.get(f"/g/{slug}/export.csv")
    assert response.status_code == 200

    import csv

    reader = csv.reader(response.text.splitlines())
    rows = list(reader)

    # Find the Balances section
    balances_idx = next(i for i, row in enumerate(rows) if row and row[0] == "Balances")
    assert balances_idx is not None
    assert rows[balances_idx + 1] == ["Member", "Balance"]

    # Parse balance rows
    balance_rows = {}
    for i in range(balances_idx + 2, len(rows)):
        if not rows[i] or not rows[i][0]:
            break
        name, balance_str = rows[i]
        # Parse decimal balance string (e.g. "46.67") to cents (4667)
        balance_cents = int(balance_str.replace(".", ""))
        balance_rows[name] = balance_cents

    # Verify exact balances from the scenario
    assert balance_rows["Chris"] == 4667
    assert balance_rows["Alex"] == -334
    assert balance_rows["Sam"] == -4333
    assert sum(balance_rows.values()) == 0


def test_csv_handles_comma_and_quote_in_description(client: TestClient):
    # Test that descriptions with commas and quotes survive the CSV round-trip.
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(
        client, slug, 'Dinner, "fancy"', "100.00", chris_id, [chris_id, alex_id, sam_id]
    )

    response = client.get(f"/g/{slug}/export.csv")
    assert response.status_code == 200

    import csv

    reader = csv.reader(response.text.splitlines())
    rows = list(reader)

    # Find the expense row and verify the description is correctly parsed
    expenses_header_idx = next(i for i, row in enumerate(rows) if row and row[0] == "Expenses")
    expense_rows = []
    for i in range(expenses_header_idx + 2, len(rows)):
        if not rows[i] or not rows[i][0]:
            break
        expense_rows.append(rows[i])

    assert len(expense_rows) > 0
    assert expense_rows[0][1] == 'Dinner, "fancy"'  # Description is second column


def test_csv_soft_deleted_expense_excluded_from_balances(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Snacks", "30.00", chris_id, [chris_id, alex_id, sam_id])

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expense = session.exec(select(Expense).where(Expense.group_id == group.id)).first()
        expense_id = expense.id

    client.post(f"/g/{slug}/expenses/{expense_id}/delete", follow_redirects=False)

    response = client.get(f"/g/{slug}/export.csv")
    assert response.status_code == 200

    import csv

    reader = csv.reader(response.text.splitlines())
    rows = list(reader)

    # Find the Balances section
    balances_idx = next(i for i, row in enumerate(rows) if row and row[0] == "Balances")
    assert rows[balances_idx + 1] == ["Member", "Balance"]

    # Parse balance rows
    balance_rows = {}
    for i in range(balances_idx + 2, len(rows)):
        if not rows[i] or not rows[i][0]:
            break
        name, balance_str = rows[i]
        balance_cents = int(balance_str.replace(".", ""))
        balance_rows[name] = balance_cents

    # All balances should be zero since the only expense is deleted
    assert all(b == 0 for b in balance_rows.values())


def test_csv_export_unidentified_visitor_redirects(client: TestClient):
    slug = _create_group(client)
    client.post(f"{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    fresh = TestClient(client.app)
    response = fresh.get(f"/g/{slug}/export.csv", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/g/{slug}/identify"


# ---------------------------------------------------------------------------
# Mobile-first polish: graceful-degradation guarantees that
# only a rendered-HTML check can catch — a future refactor could easily flip
# these silently (e.g. un-hiding the copy button by default), which is
# exactly the "dead button, no fallback" failure mode the plan calls out.
# ---------------------------------------------------------------------------


def test_identify_page_shows_empty_state_for_a_brand_new_group(client: TestClient):
    slug = _create_group(client)
    page = client.get(f"/g/{slug}/identify")
    assert page.status_code == 200
    assert "no one" in page.text.lower() and "joined" in page.text.lower()


def test_group_page_copy_button_and_loading_indicator_are_hidden_without_js(client: TestClient):
    # Both must be present-but-hidden by default: the `hidden` attribute is
    # what makes JS-off and slow-clipboard-API degrade safely. The share
    # link input itself must stay selectable as the manual-copy fallback.
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    page = client.get(f"/g/{slug}")
    assert page.status_code == 200
    assert '<button type="button" id="copy-link-btn" hidden>' in page.text
    assert 'id="loading-indicator"' in page.text and "hidden>" in page.text
    assert 'id="link" readonly' in page.text and 'onclick="this.select()"' in page.text


# ---------------------------------------------------------------------------
# Categories and the activity-feed filter
# ---------------------------------------------------------------------------


def _first_use(name: str) -> dict:
    """Form fields that create a category. There are no built-in categories,
    so a name the group hasn't used yet can only arrive through Custom…;
    afterwards it's a listed option and `category=name` picks it."""
    return {"category": "__custom__", "custom_category": name}


def _expense_by_description(slug: str, description: str) -> Expense:
    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        return session.exec(
            select(Expense).where(
                Expense.group_id == group.id, Expense.description == description
            )
        ).first()


def test_expense_stores_a_default_category_and_shows_it_as_a_chip(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))

    assert _expense_by_description(slug, "Dinner").category == "Food"
    page = client.get(f"/g/{slug}")
    assert '<span class="category-chip">Food</span>' in page.text


def test_custom_category_is_stored_exactly_as_typed(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(
        client, slug, "Lift", "30.00", chris_id, [chris_id, alex_id],
        category="__custom__", custom_category="Ski passes",
    )

    assert _expense_by_description(slug, "Lift").category == "Ski passes"
    assert "Ski passes" in client.get(f"/g/{slug}").text


def test_blank_category_is_stored_as_none_and_gets_an_uncategorized_tab(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], category="")

    assert _expense_by_description(slug, "Dinner").category is None
    page = client.get(f"/g/{slug}")
    assert "Uncategorized" in page.text
    assert "category-chip" not in page.text


def test_category_casing_folds_onto_the_existing_spelling(client: TestClient):
    # "food" typed today must join yesterday's "Food" tab, not open a second
    # one beside it.
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))
    _add_expense(
        client, slug, "Lunch", "12.00", chris_id, [chris_id, alex_id],
        category="__custom__", custom_category="  fOOd ",
    )

    assert _expense_by_description(slug, "Lunch").category == "Food"
    page = client.get(f"/g/{slug}")
    assert page.text.count(f'href="/g/{slug}?category=Food"') == 1


def test_filter_narrows_the_feed_to_one_category(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))
    _add_expense(client, slug, "Petrol", "40.00", chris_id, [chris_id, alex_id], **_first_use("Gas"))

    page = client.get(f"/g/{slug}?category=Food")
    assert page.status_code == 200
    assert "Dinner" in page.text
    assert "Petrol" not in page.text


def test_empty_category_param_shows_only_uncategorized_expenses(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))
    _add_expense(client, slug, "Mystery", "9.00", chris_id, [chris_id, alex_id])

    page = client.get(f"/g/{slug}?category=")
    assert page.status_code == 200
    assert "Mystery" in page.text
    assert "Dinner" not in page.text


def test_filtering_never_changes_the_balances(client: TestClient):
    """The whole point of filtering the feed only: what someone owes must not
    depend on which tab happens to be open."""
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "100.00", chris_id, [chris_id, alex_id, sam_id], **_first_use("Food"))
    _add_expense(client, slug, "Petrol", "45.00", alex_id, [chris_id, alex_id, sam_id], **_first_use("Gas"))

    def balances_block(text: str) -> str:
        return text.split("<h2>Balances</h2>")[1].split("</section>")[0]

    unfiltered = balances_block(client.get(f"/g/{slug}").text)
    for query in ("?category=Food", "?category=Gas", "?category="):
        assert balances_block(client.get(f"/g/{slug}{query}").text) == unfiltered


def test_settlements_appear_under_all_but_never_under_a_category(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id, sam_id], **_first_use("Food"))
    alex = _identify_as(client, slug, alex_id)
    settle = alex.post(
        f"/g/{slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(alex_id), "amount": "5.00"},
        follow_redirects=False,
    )
    assert settle.status_code == 303

    # Match the feed row itself, not the bare word "settlement" — the
    # "Suggested settlements" card in the sidebar renders on every view.
    row = "Alex paid Chris"
    assert row in client.get(f"/g/{slug}").text
    assert row not in client.get(f"/g/{slug}?category=Food").text
    assert row not in client.get(f"/g/{slug}?category=").text


def test_editing_can_change_and_then_clear_a_category(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))
    expense_id = _only_expense_id(slug)

    edit_data = {
        "description": "Dinner",
        "amount": "30.00",
        "payer_id": str(chris_id),
        "split_type": "equal",
        "participant_ids": [str(chris_id), str(alex_id)],
    }
    client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={**edit_data, **_first_use("Hotels")},
        follow_redirects=False,
    )
    assert _expense_by_description(slug, "Dinner").category == "Hotels"

    # Clearing the field must clear the category, not leave the old one stuck.
    client.post(
        f"/g/{slug}/expenses/{expense_id}/edit",
        data={**edit_data, "category": ""},
        follow_redirects=False,
    )
    assert _expense_by_description(slug, "Dinner").category is None


def test_edit_form_prefills_the_stored_category(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Hotels"))

    page = client.get(f"/g/{slug}/expenses/{_only_expense_id(slug)}/edit")
    assert 'id="category"' in page.text
    assert 'value="Hotels"' in page.text


def test_unknown_category_filter_renders_an_empty_state_not_a_500(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))

    page = client.get(f"/g/{slug}?category=DoesNotExist")
    assert page.status_code == 200
    assert "Nothing in DoesNotExist yet." in page.text


def test_over_long_category_is_truncated_not_rejected(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    # _add_expense asserts the 303, so reaching the line below is itself the
    # "not rejected" half of this test.
    _add_expense(
        client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id],
        category="__custom__", custom_category="x" * 45,
    )

    assert _expense_by_description(slug, "Dinner").category == "x" * 40


def test_csv_export_includes_the_category_column(client: TestClient):
    import csv

    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))
    _add_expense(client, slug, "Mystery", "9.00", chris_id, [chris_id, alex_id])

    rows = list(csv.reader(client.get(f"/g/{slug}/export.csv").text.splitlines()))
    assert rows[1][2] == "Category"
    by_description = {row[1]: row for row in rows[2:] if len(row) > 2}
    assert by_description["Dinner"][2] == "Food"
    assert by_description["Mystery"][2] == ""


def _category_option_values(page_text: str) -> list[str]:
    """The category <select>'s option values, in render order. Trimmed at the
    Custom… option because the payer select follows it in the markup."""
    options = re.findall(r'<option value="([^"]*)"[^>]*>([^<]*)</option>', page_text)
    trimmed = options[: options.index(("__custom__", "Custom&hellip;")) + 1]
    return [value for value, _label in trimmed]


def test_category_dropdown_starts_empty_and_grows_from_the_groups_own_use(client: TestClient):
    """No built-in starter list: a brand-new group offers only "No category"
    and Custom…, and every option after that is one the group created."""
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)

    page = client.get(f"/g/{slug}/expenses/new")
    assert '<select id="category" name="category">' in page.text
    assert '<option value="">No category</option>' in page.text
    assert _category_option_values(page.text) == ["", "__custom__"]

    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Food"))
    grown = client.get(f"/g/{slug}/expenses/new")
    assert _category_option_values(grown.text) == ["", "Food", "__custom__"]


def test_a_custom_category_joins_the_dropdown_for_that_group_only(client: TestClient):
    """The "for this trip only" guarantee: _category_options reads used
    categories from this group's expenses, so nothing leaks between groups."""
    slug = _create_group(client, "Ski trip")
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(
        client, slug, "Lift", "30.00", chris_id, [chris_id, alex_id],
        category="__custom__", custom_category="Ski passes",
    )

    assert '<option value="Ski passes"' in client.get(f"/g/{slug}/expenses/new").text

    other_slug = _create_group(client, "Beach trip")
    _create_trio(client, other_slug)
    # Match the <option>, not the bare name — the custom field's placeholder
    # text happens to use "Ski passes" as its example.
    assert '<option value="Ski passes"' not in client.get(f"/g/{other_slug}/expenses/new").text


def test_custom_selected_with_a_blank_name_is_rejected_without_500(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Dinner",
            "amount": "30.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id)],
            "category": "__custom__",
            "custom_category": "   ",
        },
    )
    assert response.status_code == 400
    assert "Enter a name for the custom category." in response.text
    # The rest of the form comes back filled in, and nothing was written.
    assert 'value="Dinner"' in response.text
    assert _expense_by_description(slug, "Dinner") is None


def test_category_not_on_the_list_is_rejected(client: TestClient):
    # A <select> constrains the browser, not a hand-written POST.
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Dinner",
            "amount": "30.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id)],
            "category": "NeverSeenBefore",
        },
    )
    assert response.status_code == 400
    assert "Choose a category from the list." in response.text
    assert _expense_by_description(slug, "Dinner") is None


def test_the_custom_sentinel_cannot_be_stored_as_a_category_name(client: TestClient):
    # Otherwise its <option value="__custom__"> would read back as "Custom…".
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)

    response = client.post(
        f"/g/{slug}/expenses/new",
        data={
            "description": "Dinner",
            "amount": "30.00",
            "payer_id": str(chris_id),
            "split_type": "equal",
            "participant_ids": [str(chris_id), str(alex_id)],
            "category": "__custom__",
            "custom_category": "__custom__",
        },
    )
    assert response.status_code == 400
    assert "reserved" in response.text
    assert _expense_by_description(slug, "Dinner") is None


def test_edit_form_preselects_the_stored_category_option(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Dinner", "30.00", chris_id, [chris_id, alex_id], **_first_use("Hotels"))

    page = client.get(f"/g/{slug}/expenses/{_only_expense_id(slug)}/edit")
    assert '<option value="Hotels" selected>Hotels</option>' in page.text


def test_custom_category_field_is_reachable_without_js(client: TestClient):
    # Mirrors .share-input: the field is rendered visible and only JS ever
    # hides it, so with scripting off the dropdown and the box are both usable.
    slug = _create_group(client)
    _create_trio(client, slug)

    page = client.get(f"/g/{slug}/expenses/new")
    assert 'id="custom_category"' in page.text
    assert "hidden" not in page.text.split('id="custom_category"')[1].split(">")[0]
    assert "category-known" not in page.text


# ---------------------------------------------------------------------------
# CSV import. The export is the import's contract, so the tests that matter
# most run a real export through a real import and compare balances.
# ---------------------------------------------------------------------------


def _import_csv(client: TestClient, slug: str, text: str):
    """Both halves of the two-step flow. Returns the preview response when it
    fails, so a caller can assert on it; otherwise the confirm redirect."""
    preview = client.post(
        f"/g/{slug}/import/preview",
        files={"file": ("export.csv", text.encode("utf-8"), "text/csv")},
        follow_redirects=False,
    )
    if preview.status_code != 200:
        return preview
    return client.post(
        f"/g/{slug}/import/confirm", data={"csv_text": text}, follow_redirects=False
    )


def _members_by_name(slug: str) -> dict[str, int]:
    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        return {m.name: m.id for m in session.exec(select(Member).where(Member.group_id == group.id))}


def _balances_by_name(slug: str) -> dict[str, int]:
    ids_to_names = {member_id: name for name, member_id in _members_by_name(slug).items()}
    balances = _balances(slug)
    # net_balances only returns members who appear in an expense or settlement;
    # everyone else is a real zero, and the two groups must agree on those too.
    return {name: balances.get(member_id, 0) for member_id, name in ids_to_names.items()}


def test_export_then_import_into_a_fresh_group_reproduces_every_balance(client: TestClient):
    source_slug = _create_group(client, "Source")
    chris_id, _alex_id, sam_id = _setup_dinner_and_taxi(client, source_slug)
    sam_browser = _identify_as(client, source_slug, sam_id)
    sam_browser.post(
        f"/g/{source_slug}/settle",
        data={"to_member_id": str(chris_id), "from_member_id": str(sam_id), "amount": "13.33", "note": "venmo"},
        follow_redirects=False,
    )
    exported = client.get(f"/g/{source_slug}/export.csv").text

    target_slug = _create_group(client, "Target")
    client.post(f"/g/{target_slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)
    response = _import_csv(client, target_slug, exported)
    assert response.status_code == 303

    source_balances = _balances_by_name(source_slug)
    target_balances = _balances_by_name(target_slug)
    # The importer identified themselves before uploading and is in no row of
    # the file, so they are a real zero rather than a missing member.
    assert target_balances.pop("Importer") == 0
    assert target_balances == source_balances
    assert sorted(source_balances) == ["Alex", "Chris", "Sam"]


def test_import_creates_the_members_named_in_the_file(client: TestClient):
    source_slug = _create_group(client, "Source")
    _setup_dinner_and_taxi(client, source_slug)
    exported = client.get(f"/g/{source_slug}/export.csv").text

    target_slug = _create_group(client, "Target")
    client.post(f"/g/{target_slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)
    assert _import_csv(client, target_slug, exported).status_code == 303

    # Chris already existed and is matched, not duplicated; Alex and Sam are new.
    assert sorted(_members_by_name(target_slug)) == ["Alex", "Chris", "Sam"]


def test_import_preserves_the_dates_and_order_from_the_file(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)
    _import_csv(
        client,
        slug,
        "Expenses\n"
        "Date,Description,Amount,Payer,Split Type,Participants & Shares\n"
        "2026-08-14,Newest,10.00,Chris,equal,Chris: 10.00\n"
        "2026-08-14,Middle,10.00,Chris,equal,Chris: 10.00\n"
        "2026-08-01,Oldest,10.00,Chris,equal,Chris: 10.00\n",
    )

    with Session(engine) as session:
        group = session.exec(select(Group).where(Group.slug == slug)).first()
        expenses = session.exec(
            select(Expense).where(Expense.group_id == group.id).order_by(Expense.created_at.desc())
        ).all()

    assert [e.description for e in expenses] == ["Newest", "Middle", "Oldest"]
    assert [e.created_at.date().isoformat() for e in expenses] == ["2026-08-14", "2026-08-14", "2026-08-01"]
    # Same date, distinct timestamps — otherwise the feed's ordering of the
    # two 2026-08-14 rows would be arbitrary.
    assert expenses[0].created_at != expenses[1].created_at


def test_import_folds_a_category_onto_an_existing_spelling(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _create_trio(client, slug)
    _add_expense(client, slug, "Hotel", "30.00", chris_id, [chris_id, alex_id], **_first_use("Lodging"))

    _import_csv(
        client,
        slug,
        "Expenses\n"
        "Date,Description,Category,Amount,Payer,Split Type,Participants & Shares\n"
        "2026-08-14,Motel,lodging,10.00,Chris,equal,Chris: 10.00\n",
    )

    assert _expense_by_description(slug, "Motel").category == "Lodging"


def test_preview_writes_nothing(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)

    response = client.post(
        f"/g/{slug}/import/preview",
        files={
            "file": (
                "export.csv",
                b"Expenses\nDate,Description,Amount,Payer,Split Type,Participants & Shares\n"
                b"2026-08-14,Dinner,10.00,Chris,equal,Chris: 10.00\n",
                "text/csv",
            )
        },
    )

    assert response.status_code == 200
    assert "Dinner" in response.text
    assert "Chris" in response.text
    assert _expense_by_description(slug, "Dinner") is None
    assert sorted(_members_by_name(slug)) == ["Importer"]


def test_a_file_with_a_bad_row_is_refused_and_writes_nothing(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)

    response = _import_csv(
        client,
        slug,
        "Expenses\n"
        "Date,Description,Amount,Payer,Split Type,Participants & Shares\n"
        "2026-08-14,Dinner,10.00,Chris,equal,Chris: 4.00; Alex: 5.00\n",
    )

    assert response.status_code == 400
    assert "Row 3" in response.text
    assert _expense_by_description(slug, "Dinner") is None
    assert sorted(_members_by_name(slug)) == ["Importer"]


def test_confirm_revalidates_rather_than_trusting_the_hidden_field(client: TestClient):
    """The CSV crosses the two steps through the browser, so the confirm route
    re-parses it instead of taking it on trust."""
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)

    response = client.post(
        f"/g/{slug}/import/confirm",
        data={
            "csv_text": "Expenses\nDate,Description,Amount,Payer,Split Type,Participants & Shares\n"
            "2026-08-14,Tampered,10.00,Chris,equal,Chris: 99.00\n"
        },
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _expense_by_description(slug, "Tampered") is None


def test_a_file_that_is_not_an_export_is_refused(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)

    response = _import_csv(client, slug, "name,total\nDinner,100\n")

    assert response.status_code == 400
    assert "doesn&#39;t look like an exported CSV" in response.text


def test_import_appends_and_leaves_existing_rows_alone(client: TestClient):
    slug = _create_group(client)
    chris_id, alex_id, _sam_id = _setup_dinner_and_taxi(client, slug)
    before = _balances(slug)

    _import_csv(
        client,
        slug,
        "Expenses\n"
        "Date,Description,Amount,Payer,Split Type,Participants & Shares\n"
        "2026-08-14,Breakfast,10.00,Chris,exact,Chris: 5.00; Alex: 5.00\n",
    )

    after = _balances(slug)
    assert _expense_by_description(slug, "Dinner") is not None
    assert after[chris_id] == before[chris_id] + 500
    assert after[alex_id] == before[alex_id] - 500


def test_import_page_bounces_an_unidentified_visitor(client: TestClient):
    slug = _create_group(client)
    stranger = TestClient(client.app)

    response = stranger.get(f"/g/{slug}/import", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == f"/g/{slug}/identify"


def test_import_flash_counts_read_as_a_sentence(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Importer"}, follow_redirects=False)

    response = _import_csv(
        client,
        slug,
        "Expenses\n"
        "Date,Description,Amount,Payer,Split Type,Participants & Shares\n"
        "2026-08-14,Dinner,10.00,Chris,equal,Chris: 10.00\n"
        "\nSettlements\nDate,From,To,Amount\n2026-08-15,Alex,Chris,5.00\n",
    )

    assert response.status_code == 303
    assert "Imported%201%20expense%2C%201%20settlement%2C%202%20new%20members." in response.headers["location"]


def test_dashboard_links_to_the_import_page(client: TestClient):
    slug = _create_group(client)
    client.post(f"/g/{slug}/identify", data={"new_name": "Chris"}, follow_redirects=False)

    page = client.get(f"/g/{slug}")

    assert f'href="/g/{slug}/import"' in page.text
