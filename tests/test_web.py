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
    draft = client.get(r.headers["location"]).text
    assert "status-draft" in draft and "Submit" in draft
    assert client.post(f"/requests/{rid}/steps/do_the_thing/complete").status_code == 409  # still a Draft
    assert client.post(f"/requests/{rid}/submit", follow_redirects=False).status_code == 303
    assert "REQ-" in client.get(f"/requests/{rid}").text

    assert client.post(f"/requests/{rid}/steps/do_the_thing/complete").status_code == 409  # items unticked

    for i in range(3):
        client.post(f"/requests/{rid}/steps/do_the_thing/checklist/{i}", data={"checked": "true"})
    detail = client.post(f"/requests/{rid}/steps/do_the_thing/complete")
    assert detail.status_code == 200 and "Completed" in detail.text

    assert "Add user jdoe" not in client.get("/").text  # the default view is open requests
    listing = client.get("/?status=completed").text
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
    services.submit(rid, {})
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


def forms_client(share, tmp_path, forms_registry):
    from tests.conftest import make_services
    svc = make_services(share, tmp_path / "f.sqlite", forms_registry)
    return svc, client_for(svc)


def test_draft_form_save_submit_with_errors_then_success(share, tmp_path, forms_registry):
    svc, client = forms_client(share, tmp_path, forms_registry)
    rid = client.post("/requests", data={"workflow": "limits@1", "title": "ACC1"},
                      follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/requests/{rid}").text
    assert 'name="capture.account"' in page and 'name="capture.limits.max"' in page and "Save draft" in page

    r = client.post(f"/requests/{rid}/forms", data={"_action": "save", "capture.account": "ACC1"},
                    follow_redirects=False)
    assert r.status_code == 303 and svc.get(rid)[0].step("capture").draft == {"account": "ACC1"}

    bad = client.post(f"/requests/{rid}/forms", data={"_action": "submit", "capture.account": "ACC1",
                                                      "capture.limits.max": "lots"})
    assert bad.status_code == 422
    assert "Enter a number" in bad.text and 'value="lots"' in bad.text and 'value="ACC1"' in bad.text
    assert 'data-autorefresh="false"' in bad.text  # never auto-reload (and re-POST) an error page
    assert 'data-autorefresh="true"' in client.get(f"/requests/{rid}").text
    assert svc.get(rid)[0].status == "Draft"

    ok = client.post(f"/requests/{rid}/forms", data={"_action": "submit", "capture.account": "ACC1",
                                                     "capture.limits.max": "10"})
    assert ok.status_code == 200 and "LIM-" in ok.text and "Max position" in ok.text
    state = svc.get(rid)[0]
    assert state.status == "Active" and state.inputs == {"account": "ACC1", "limits": {"max": 10}}
    assert state.reference in client.get("/").text


def test_ready_form_step_and_notes(share, tmp_path, forms_registry):
    svc, client = forms_client(share, tmp_path, forms_registry)
    rid = svc.create_request("limits@1", "x")
    svc.submit(rid, {"capture": {"account": "A", "limits": {"max": 1}}})
    svc.set_checklist_item(rid, "apply", 0, True)
    svc.complete_step(rid, "apply")
    page = client.get(f"/requests/{rid}").text
    assert 'name="record.ticket"' in page and 'name="_step" value="record"' in page
    bad = client.post(f"/requests/{rid}/forms", data={"_step": "record", "_action": "submit"})
    assert bad.status_code == 422 and "Required" in bad.text
    client.post(f"/requests/{rid}/forms", data={"_step": "record", "_action": "submit", "record.ticket": "K-9"})
    assert svc.get(rid)[0].status == "Completed"

    r = client.post(f"/requests/{rid}/notes", data={"text": "Done & dusted <b>"}, follow_redirects=False)
    assert r.status_code == 303
    page = client.get(f"/requests/{rid}").text
    assert "Done &amp; dusted &lt;b&gt;" in page and "note added" in page


def test_tasks_page_and_claim_returns_there(services):
    client = client_for(services)
    rid = services.create_request(DEMO, "Needs doing")
    services.submit(rid, {})
    page = client.get("/tasks").text
    assert "Unclaimed on my requests" in page and "Needs doing" in page
    r = client.post(f"/requests/{rid}/steps/do_the_thing/claim", headers={"Referer": f"{ORIGIN}/tasks"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tasks"
    page = client.get("/tasks").text
    assert 'class="badge">1<' in page  # nav count of steps with me
    services.release_step(rid, "do_the_thing")
    r = client.post(f"/requests/{rid}/steps/do_the_thing/claim", headers={"Referer": "https://evil.example/tasks"},
                    follow_redirects=False)
    assert r.headers["location"] == "/tasks"  # only ever a path on this app, never another site
    services.release_step(rid, "do_the_thing")
    r = client.post(f"/requests/{rid}/steps/do_the_thing/claim", headers={"Referer": "https://evil.example/x"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/requests/{rid}"


def test_request_page_owner_and_assignment_controls(services):
    client = client_for(services)
    rid = services.create_request(DEMO, "x")
    services.submit(rid, {})
    page = client.get(f"/requests/{rid}").text
    assert "Owner: <strong>alice</strong>" in page and "Unclaimed" in page and ">Claim<" in page
    client.post(f"/requests/{rid}/steps/do_the_thing/assign", data={"user": "bob"})
    client.post(f"/requests/{rid}/owner", data={"owner": "bob"})
    page = client.get(f"/requests/{rid}").text
    assert "With <strong>bob</strong>" in page and "Owner: <strong>bob</strong>" in page
    assert "<td>bob</td>" in client.get("/").text


def test_wait_resume_and_waiting_board_pages(services):
    client = client_for(services)
    rid = services.create_request(DEMO, "Needs a form")
    services.submit(rid, {})
    page = client.get(f"/requests/{rid}").text
    assert ">Start<" in page and "Mark as waiting" in page and "pill-ready" in page
    client.post(f"/requests/{rid}/steps/do_the_thing/start")
    assert "pill-in_progress" in client.get(f"/requests/{rid}").text
    client.post(f"/requests/{rid}/steps/do_the_thing/wait", data={"reason": "client", "note": "signed form"})
    page = client.get(f"/requests/{rid}").text
    assert "Waiting on <strong>Client</strong>: signed form" in page and ">Resume<" in page
    assert "/checklist/0" not in page  # no ticking while waiting
    assert "waiting on client" in page  # timings line
    board = client.get("/waiting").text
    assert "Needs a form" in board and "signed form" in board and "Nothing waiting on krm22" in board
    r = client.post(f"/requests/{rid}/steps/do_the_thing/resume", headers={"Referer": f"{ORIGIN}/waiting"},
                    follow_redirects=False)
    assert r.headers["location"] == "/waiting"
    assert "Needs a form" not in client.get("/waiting").text
    assert client.post(f"/requests/{rid}/steps/do_the_thing/wait", data={"reason": "other"}).status_code == 409


def test_request_list_filters_and_sort_links(services):
    client = client_for(services)
    for title in ("Zulu", "Alpha"):
        rid = services.create_request(DEMO, title)
        services.submit(rid, {})
    page = client.get("/?status=active&sort=title&dir=asc").text
    assert page.index("Alpha") < page.index("Zulu")
    assert 'aria-sort="ascending"' in page and "sort=title&amp;dir=desc" in page
    page = client.get("/?status=active&sort=title&dir=desc").text
    assert page.index("Zulu") < page.index("Alpha")
    assert "No requests match" in client.get("/?q=nothing-like-this").text
    assert ">Age" in page and "In step" in page and "pill-ready" in page


def test_upload_and_download_attachments(services):
    client = client_for(services)
    rid = services.create_request(DEMO, "x")
    page = client.get(f"/requests/{rid}").text
    assert 'enctype="multipart/form-data"' in page and "No files attached" in page
    r = client.post(f"/requests/{rid}/attachments", files={"file": ("Client email.msg", b"hello", "application/vnd.ms-outlook")},
                    data={"step": "do_the_thing"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#attachments")
    page = client.get(f"/requests/{rid}").text
    assert "Client email.msg" in page and "5 bytes" in page and "attachment added" in page
    sha = services.get(rid)[0].attachments[0].sha256
    dl = client.get(f"/requests/{rid}/attachments/{sha}")
    assert dl.status_code == 200 and dl.content == b"hello"
    assert "attachment" in dl.headers["content-disposition"] and "Client%20email.msg" in dl.headers["content-disposition"]
    assert client.get(f"/requests/{rid}/attachments/{'0' * 64}").status_code == 404
    assert client.post(f"/requests/{rid}/attachments", data={"step": ""}).status_code == 409


def test_oversized_upload_is_refused_up_front(services):
    client = client_for(services)
    rid = services.create_request(DEMO, "x")
    r = client.post(f"/requests/{rid}/attachments", content=b"x",
                    headers={"Content-Length": str(40 * 1024 * 1024), "Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 413 and "limit is 25 MB" in r.text
    services.attachments.max_bytes = 4
    r = client.post(f"/requests/{rid}/attachments", files={"file": ("big.bin", b"12345")})
    assert r.status_code == 413 and "larger than the 0 MB limit" in r.text


def test_hold_cancel_flags_and_follow_up_pages(share, tmp_path, krm_registry):
    from tests.conftest import make_services
    svc = make_services(share, tmp_path / "k.sqlite", krm_registry)
    client = client_for(svc)
    rid = svc.create_request("krm@1", "ACC1 change")
    svc.submit(rid, {"capture": {"account": "ACC1"}})
    svc.start_step(rid, "raise")
    page = client.get(f"/requests/{rid}").text
    assert "Put on hold" in page and "Cancel request" in page
    assert client.post(f"/requests/{rid}/hold", data={"reason": ""}).status_code == 409
    client.post(f"/requests/{rid}/hold", data={"reason": "Client paused"})
    page = client.get(f"/requests/{rid}").text
    assert "<strong>On hold</strong>" in page and "Client paused" in page and "KRM22 may still approve ACC1" in page
    assert 'class="badge badge-warn">1<' in page
    client.post(f"/requests/{rid}/cancel", data={"reason": "Client withdrew"})
    page = client.get(f"/requests/{rid}").text
    assert "<strong>Cancelling</strong>" in page and "Withdraw KRM22 request for ACC1" in page
    flags = client.get("/flags").text
    assert "ACC1 change" in flags and "Withdraw the ACC1 request in KRM22" in flags
    for f in svc.get(rid)[0].flags:
        r = client.post(f"/requests/{rid}/flags/resolve", data={"flag": f.id, "resolution": "actioned", "notes": "Done"},
                        headers={"Referer": f"{ORIGIN}/flags"}, follow_redirects=False)
        assert r.headers["location"] == "/flags"
    page = client.get(f"/requests/{rid}").text
    assert "<strong>Cancelled</strong>" in page and "Create follow-up request" in page
    assert "Reassign" not in page and "Put on hold" not in page  # nothing to change on a finished request
    assert "No open flags" in client.get("/flags").text
    r = client.post(f"/requests/{rid}/follow-up", data={"title": ""}, follow_redirects=False)
    follow_page = client.get(r.headers["location"]).text
    assert "Follow-up to" in follow_page and "status-draft" in follow_page
    assert "Follow-ups:" in client.get(f"/requests/{rid}").text
    assert ">Cancelled<" in client.get("/?status=cancelled").text or "Cancelled" in client.get("/?status=cancelled").text
