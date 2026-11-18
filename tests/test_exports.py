"""Request record page and Excel exports (#18)."""
import io
from datetime import datetime

import pytest
from openpyxl import load_workbook

from tests.conftest import DEMO, make_services
from tests.test_web import client_for
from wft import exports

STEP = "do_the_thing"


def book(data: bytes):
    return load_workbook(io.BytesIO(data))


def rows(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


@pytest.fixture
def worked(services):
    """A completed request that waited on the client and was on hold, plus an open one."""
    rid = services.create_request(DEMO, "=HYPERLINK(\"http://evil.example.invalid\",\"click\")")
    services.submit(rid, {})
    services.wait_step(rid, STEP, "client", "signed form")
    services.resume_step(rid, STEP)
    services.hold(rid, "client paused")
    services.resume(rid)
    for i in range(3):
        services.set_checklist_item(rid, STEP, i, True)
    services.complete_step(rid, STEP)
    services.add_note(rid, "All done")
    other = services.create_request(DEMO, "Still open")
    services.submit(other, {})
    return rid, other


def test_request_list_export(services, worked):
    rid, other = worked
    wb = book(services.export_requests(status="all"))
    ws = wb["Requests"]
    data = rows(ws)
    assert data[0] == exports.REQUEST_COLUMNS
    by_title = {r[1]: dict(zip(data[0], r)) for r in data[1:]}
    done = by_title['=HYPERLINK("http://evil.example.invalid","click")']
    assert done["Status"] == "Completed" and done["Reference"].startswith("REQ-") and done["Notes"] == 1
    assert isinstance(done["Created (UTC)"], datetime) and isinstance(done["Finished (UTC)"], datetime)
    assert by_title["Still open"]["Current step"] == "Do the thing" and by_title["Still open"]["Finished (UTC)"] is None
    # Text that looks like a formula is stored as text, never as a formula.
    cell = next(c for c in ws["B"] if c.value and str(c.value).startswith("=HYPERLINK"))
    assert cell.data_type == "s"
    assert ws.freeze_panes == "A2" and ws.auto_filter.ref


def test_export_follows_the_list_filters(services, worked):
    data = rows(book(services.export_requests(status="open"))["Requests"])
    assert [r[1] for r in data[1:]] == ["Still open"]
    assert len(rows(book(services.export_requests(status="completed"))["Requests"])) == 2


def test_step_timings_export(services, worked):
    rid, _ = worked
    wb = book(services.export_timings(status="all"))
    header, *body = rows(wb["Step timings"])
    assert header == exports.timing_columns()
    assert "Waiting: Client (h)" in header and "On hold (h)" in header and "Working (h)" in header
    done = next(dict(zip(header, r)) for r in body if r[0] == services.get(rid)[0].reference and r[4] == STEP)
    assert done["Workflow"] == "Demo manual checklist" and done["Version"] == 1 and done["Step status"] == "done"
    assert done["Waiting: Client (h)"] >= 0 and done["Waiting: KRM22 (h)"] == 0 and done["On hold (h)"] >= 0
    assert done["Transitions"] >= 5 and isinstance(done["Finished (UTC)"], datetime)
    t_header, *t_body = rows(wb["Transitions"])
    assert t_header == exports.TRANSITION_COLUMNS
    states = [r[3] for r in t_body if r[0] == done["Reference"] and r[2] == STEP]
    assert states[:4] == ["ready", "in progress", "waiting", "in progress"] and states[-1] == "done"
    waiting = next(r for r in t_body if r[0] == done["Reference"] and r[3] == "waiting")
    assert waiting[6] == "client" and waiting[7] == "signed form"


def test_timing_hours_are_exact():
    from tests.test_timings import Run, at
    from wft import engine
    from wft.definitions import DefinitionRegistry
    from wft.config import REPO_ROOT
    registry = DefinitionRegistry.from_repo(REPO_ROOT / "definitions")
    r = Run(registry.get(DEMO))
    d = r.defn
    r.do(engine.create_request(d, "t"), at("08:00")).do(engine.submit_request(r.state, d, {}), at("09:00"))
    r.do(engine.wait_step(r.state, d, STEP, "krm22"), at("09:30"))
    r.do(engine.resume_step(r.state, d, STEP), at("11:30"))
    r.do(engine.hold_request(r.state, d, "x"), at("12:00"))
    r.do(engine.resume_request(r.state, d), at("13:00"))
    for i in range(3):
        r.do(engine.set_checklist_item(r.state, d, STEP, i, True), at("13:15"))
    r.do(engine.complete_step(r.state, d, STEP), at("13:30"))
    [row] = exports.timing_rows([r.state], registry, lambda u: u)
    cols = dict(zip(exports.timing_columns(), row))
    assert cols["Waiting: KRM22 (h)"] == 2.0 and cols["On hold (h)"] == 1.0
    assert cols["Working (h)"] == 1.0 and cols["Elapsed (h)"] == 3.5  # 09:00-13:30 less an hour on hold


def test_record_page(services, worked):
    rid, _ = worked
    client = client_for(services)
    page = client.get(f"/requests/{rid}/record").text
    state = services.get(rid)[0]
    for text in (state.reference, "Print / Save as PDF", "Full history", "request submitted", "step waiting",
                 "client paused", "All done", "Completed by alice", "waiting on client", "on hold"):
        assert text in page, text
    assert page.count("<tr>") >= len(services.rm.history(rid))  # every event listed
    assert '=HYPERLINK("' not in page  # the title is escaped, never raw HTML or a live formula
    assert "=HYPERLINK(&#34;" in page or "=HYPERLINK(&quot;" in page
    dl = client.get(f"/requests/{rid}/record?download=1")
    assert f'filename="{state.reference}_record.html"' in dl.headers["content-disposition"]
    assert "Request record (print / PDF)" in client.get(f"/requests/{rid}").text


def test_record_lists_flags_attachments_and_overridden_actions(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    bob = make_services(share, tmp_path / "b.sqlite", registry, "bob")
    rid = alice.create_request(DEMO, "Contested")
    alice.submit(rid, {})
    alice.add_attachment(rid, "client.msg", io.BytesIO(b"hello"))
    bob.sync()
    alice.set_checklist_item(rid, STEP, 0, True)
    bob.set_checklist_item(rid, STEP, 1, True)  # loses to alice's tick
    alice.sync()
    alice.cancel(rid, "withdrawn")
    page = client_for(alice).get(f"/requests/{rid}/record").text
    assert "client.msg" in page and "Manual rollback may be required" in page and "Open" in page
    assert "Actions overridden by a simultaneous change" in page and "bob" in page


def test_export_routes(services, worked):
    client = client_for(services)
    r = client.get("/export/requests.xlsx?status=all&q=Still")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert "attachment" in r.headers["content-disposition"] and "wft-requests-uat-" in r.headers["content-disposition"]
    assert [x[1] for x in rows(book(r.content)["Requests"])[1:]] == ["Still open"]
    t = client.get("/export/timings.xlsx")
    assert t.status_code == 200 and set(book(t.content).sheetnames) == {"Step timings", "Transitions"}
    page = client.get("/?status=all&q=Still").text
    assert "/export/requests.xlsx?status=all" in page and "q=Still" in page
