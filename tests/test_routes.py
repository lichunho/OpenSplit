"""
Happy-path route flows plus the auth edges that matter: a wrong password
must never 500 (trap #2), duplicate names are rejected case-insensitively,
an unidentified visitor is bounced to identify, and switch clears only the
group it's called on.
"""
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


def _add_expense(
    client: TestClient,
    slug: str,
    description: str,
    amount: str,
    payer_id: int,
    participant_ids: list[int],
    split_type: str = "equal",
    shares: dict[int, str] | None = None,
) -> None:
    data = {
        "description": description,
        "amount": amount,
        "payer_id": str(payer_id),
        "split_type": split_type,
        "participant_ids": [str(pid) for pid in participant_ids],
    }
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
# Settle-up flow (milestone 6). Every scenario below shares the same setup as
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
# CSV export (milestone 7).
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
