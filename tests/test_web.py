from fastapi.testclient import TestClient

from tests.conftest import DEMO
from wft.web import security
from wft.web.app import create_app

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
TOKEN = "test-token"


def client_for(services, signed_in=True) -> TestClient:
    client = TestClient(create_app(services, "uat", TOKEN, PORT), base_url=ORIGIN, headers={"Origin": ORIGIN})
    if signed_in:
        client.get(f"/auth?token={TOKEN}")
    return client


def test_create_tick_and_complete_through_ui(services):
    client = client_for(services)
    page = client.get("/")
    assert page.status_code == 200 and "UAT environment" in page.text and "Demo manual checklist" in page.text
    assert "<title>[UAT]" in page.text

    r = client.post("/requests", data={"workflow": DEMO, "title": "Add user jdoe"}, follow_redirects=False)
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[1]

    assert client.post(f"/requests/{rid}/steps/do_the_thing/complete").status_code == 409

    for i in range(3):
        client.post(f"/requests/{rid}/steps/do_the_thing/checklist/{i}", data={"checked": "true"})
    detail = client.post(f"/requests/{rid}/steps/do_the_thing/complete")
    assert detail.status_code == 200 and "Completed" in detail.text

    listing = client.get("/").text
    assert "Add user jdoe" in listing and "status-completed" in listing


def test_unknown_request_is_404(services):
    assert client_for(services).get("/requests/nope").status_code == 404


def test_auth_sets_strict_cookie_and_redirects(services):
    client = client_for(services, signed_in=False)
    r = client.get(f"/auth?token={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    cookie = r.headers["set-cookie"].lower()
    assert security.cookie_name(PORT) in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_requests_without_session_are_refused(services):
    client = client_for(services, signed_in=False)
    assert client.get("/").status_code == 403
    assert client.get("/static/wft.css").status_code == 403
    assert client.get("/auth?token=wrong").status_code == 403
    assert client.post("/requests", data={"workflow": DEMO, "title": "x"}).status_code == 403
    assert services.rm.list_requests() == []


def test_session_from_another_port_is_refused(services):
    client = client_for(services, signed_in=False)
    client.cookies.set(security.cookie_name(PORT + 1), TOKEN)
    assert client.get("/").status_code == 403


def test_foreign_host_header_is_refused(services):
    client = client_for(services)
    assert client.get("/", headers={"Host": "evil.example:8765"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:9999"}).status_code == 400
    assert client.get("/", headers={"Host": f"localhost:{PORT}"}).status_code == 200


def test_state_changes_need_same_origin(services):
    client = client_for(services)
    form = {"workflow": DEMO, "title": "x"}
    assert client.post("/requests", data=form, headers={"Origin": "https://evil.example"}).status_code == 403
    no_origin = client.post("/requests", data=form, headers={"Origin": ""})
    assert no_origin.status_code == 403
    by_referer = client.post("/requests", data=form, headers={"Origin": "", "Referer": f"{ORIGIN}/"},
                             follow_redirects=False)
    assert by_referer.status_code == 303
    assert len(services.rm.list_requests()) == 1


def test_status_endpoint_tracks_changes(services):
    client = client_for(services)
    before = client.get("/status").json()
    assert before == {"version": 0, "read_only": False, "conflicts": 0}
    services.create_request(DEMO, "x")
    assert client.get("/status").json()["version"] == 1


def test_rebuild_cache_button(services):
    client = client_for(services)
    services.create_request(DEMO, "Kept")
    assert "Rebuild cache" in client.get("/").text
    r = client.post("/cache/rebuild", follow_redirects=False)
    assert r.status_code == 303
    assert [r.title for r in services.rm.list_requests()] == ["Kept"]


def test_read_only_banner_and_blocked_writes(services, share):
    client = client_for(services)
    hidden = share.with_name("offline")
    share.rename(hidden)
    try:
        services.sync()
        page = client.get("/")
        assert "Read-only" in page.text and 'class="env-uat read-only"' in page.text
        r = client.post("/requests", data={"workflow": DEMO, "title": "x"})
        assert r.status_code == 503 and "unavailable" in r.text
    finally:
        hidden.rename(share)
    services.sync()
    assert "Read-only" not in client.get("/").text


def test_conflict_banner_and_dismiss(services, share, tmp_path, registry):
    from tests.conftest import make_services
    services._acknowledged_path = tmp_path / "ack.json"
    bob = make_services(share, tmp_path / "bob.sqlite", registry, "bob")
    rid = services.create_request(DEMO, "Contested")
    bob.sync()
    bob.set_checklist_item(rid, "do_the_thing", 0, True)
    services.set_checklist_item(rid, "do_the_thing", 1, True)  # alice hadn't seen bob's tick
    services.sync()
    client = client_for(services)
    page = client.get("/").text
    assert "was overridden" in page and "ticking item 2" in page and "Contested" in page
    [c] = services.my_conflicts()
    r = client.post(f"/conflicts/{c.event_id}/dismiss", headers={"Referer": f"{ORIGIN}/"}, follow_redirects=False)
    assert r.status_code == 303
    assert "was overridden" not in client.get("/").text
