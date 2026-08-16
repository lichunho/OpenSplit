"""
Happy-path route flows plus the auth edges that matter: a wrong password
must never 500 (trap #2), duplicate names are rejected case-insensitively,
an unidentified visitor is bounced to identify, and switch clears only the
group it's called on.
"""
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.db import engine
from app.models import Expense, Group, Member, Share


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

    page = client.get(f"/g/{slug}")
    assert page.text.lower().count(">sam<") == 1


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
