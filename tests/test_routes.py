"""
Happy-path route flows plus the auth edges that matter: a wrong password
must never 500 (trap #2), duplicate names are rejected case-insensitively,
an unidentified visitor is bounced to identify, and switch clears only the
group it's called on.
"""
from fastapi.testclient import TestClient


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
