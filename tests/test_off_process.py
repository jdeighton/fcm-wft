"""Steps done outside WFT, off-process records and their reason categories."""
import io
from datetime import date

import pytest
import yaml

from tests.conftest import DEMO, make_services
from wft import engine, offprocess, timings
from wft.configfiles import DEFAULT_REASONS, REASONS_NAME, ReloadingFile, parse_reasons, reasons_errors
from wft.engine import CommandError

TODAY = timings.now_utc().date().isoformat()


def log(svc, **kw):
    args = dict(system="TT", what="Raised ACC9 max position to 800", performed_on=TODAY, reason="urgent",
                notes="Client called at 7pm; done in TT Setup", linked_request="")
    args.update(kw)
    return svc.record_off_process(**args)


# --- steps done outside WFT -------------------------------------------------

def test_any_workable_step_can_be_done_outside_with_notes(services):
    rid = services.create_request(DEMO, "x")
    services.submit(rid, {})
    with pytest.raises(CommandError, match="Say what was done"):
        services.complete_outside_tool(rid, "do_the_thing", "  ")
    services.complete_outside_tool(rid, "do_the_thing", "Done directly in TT during the outage")
    state = services.get(rid)[0]
    step = state.step("do_the_thing")
    assert step.status == engine.DONE_OUTSIDE and step.data == {"outside_tool_notes": "Done directly in TT during the outage"}
    assert step.transitions[-1].note == "Done directly in TT during the outage"
    assert state.status == engine.COMPLETED  # counts as finished
    assert timings.step_timing(step).finished_at == step.completed_at


def test_waiting_steps_must_be_resumed_first(services):
    rid = services.create_request(DEMO, "x")
    services.submit(rid, {})
    services.wait_step(rid, "do_the_thing", "client")
    with pytest.raises(CommandError, match="resume it"):
        services.complete_outside_tool(rid, "do_the_thing", "x")


def test_steps_done_outside_are_flagged_on_cancel_and_amend(share, tmp_path, amend_registry):
    svc = make_services(share, tmp_path / "a.sqlite", amend_registry)
    rid = svc.create_request("amendable@1", "x")
    svc.submit(rid, {"capture": {"account": "A", "limits": {"max": 1}}})
    svc.complete_outside_tool(rid, "csv", "made the CSV by hand")
    svc.amend(rid, {"capture": {"account": "A", "limits": {"max": 2}}})
    assert [f.step for f in svc.get(rid)[0].flags] == ["csv"]
    svc.cancel(rid, "withdrawn")
    assert "csv" in {f.step for f in svc.get(rid)[0].flags if f.trigger == "cancel"}


# --- off-process records -------------------------------------------------------

def test_record_validation():
    ids = [r.id for r in DEFAULT_REASONS]
    today = date(2026, 11, 18)
    ok = dict(system="TT", what="x", performed_on="2026-11-18", reason="urgent", notes="n", linked_request=None,
              reason_ids=ids, today=today)
    assert offprocess.record(**ok).payload["performed_on"] == "2026-11-18"
    for change, message in [({"system": " "}, "which trading system"), ({"what": ""}, "what was set up"),
                            ({"notes": ""}, "Notes are required"), ({"reason": "bored"}, "Choose a reason"),
                            ({"performed_on": "yesterday"}, "Give the date"),
                            ({"performed_on": "2026-12-25"}, "can't be in the future")]:
        with pytest.raises(CommandError, match=message):
            offprocess.record(**{**ok, **change})


def test_record_gets_reference_and_appears_in_search(services):
    rid = log(services)
    rec = services.get_off_process(rid)
    assert rec.reference == f"OFF-{rec.recorded_at[:4]}-0001" and rec.recorded_by == "alice"
    assert rec.system == "TT" and rec.reason == "urgent" and rec.performed_on == TODAY
    log(services, system="Other", what="Added user in vendor portal", reason="tool_gap", notes="no workflow yet")
    titles = lambda recs: sorted(r.what for r in recs)  # noqa: E731
    assert titles(services.off_process_records("acc9")) == ["Raised ACC9 max position to 800"]
    assert titles(services.off_process_records(rec.reference.lower())) == ["Raised ACC9 max position to 800"]
    assert titles(services.off_process_records("missing workflow")) == ["Added user in vendor portal"]  # reason label
    assert titles(services.off_process_records(reason="tool_gap")) == ["Added user in vendor portal"]
    assert titles(services.off_process_records(system="tt")) == ["Raised ACC9 max position to 800"]
    assert services.rm.list_requests() == []  # records are not requests


def test_linking_attachments_notes_and_other_users(services, share, tmp_path, registry):
    req = services.create_request(DEMO, "Linked one")
    services.submit(req, {})
    ref = services.get(req)[0].reference
    with pytest.raises(CommandError, match="No request"):
        log(services, linked_request="NOPE-1")
    rid = log(services, linked_request=ref)
    assert services.get_off_process(rid).linked_request == req
    assert [r.record_id for r in services.off_process_for_request(req)] == [rid]
    sha = services.add_off_process_attachment(rid, "client email.msg", io.BytesIO(b"please do it now"))
    att, path = services.off_process_attachment_file(rid, sha)
    assert path.read_bytes() == b"please do it now" and att.name == "client email.msg"
    services.add_off_process_note(rid, "Confirmed with risk next morning")
    bob = make_services(share, tmp_path / "b.sqlite", registry, "bob")
    rec = bob.get_off_process(rid)
    assert rec.reference and [a.name for a in rec.attachments] == ["client email.msg"]
    assert [n.text for n in rec.extra_notes] == ["Confirmed with risk next morning"]
    services.rebuild_cache()
    assert services.get_off_process(rid).extra_notes


# --- reason categories config ---------------------------------------------------

def test_reasons_config(tmp_path):
    assert reasons_errors({"reasons": [{"id": "a", "label": "A"}, {"id": "a", "label": "again"}]}) == [
        "reason id 'a' is listed more than once"]
    assert reasons_errors({"reasons": []})
    path = tmp_path / REASONS_NAME
    src = ReloadingFile(path, parse_reasons, DEFAULT_REASONS)
    assert src.value == DEFAULT_REASONS and len(DEFAULT_REASONS) == 6
    path.write_text(yaml.safe_dump({"reasons": [{"id": "urgent", "label": "Urgent"}, {"id": "vendor", "label": "Vendor-only"}]}))
    assert src.refresh() and [r.id for r in src.value] == ["urgent", "vendor"]


def test_repo_reasons_match_the_agreed_seed():
    from wft.config import REPO_ROOT
    repo = parse_reasons(yaml.safe_load((REPO_ROOT / "definitions/config" / REASONS_NAME).read_text()))
    assert repo == DEFAULT_REASONS


def test_reasons_are_published_and_used(tmp_path, share, registry):
    from tests.defs import MANUAL, write_defs
    from wft.publishing import publish
    reasons = {"reasons": [{"id": "vendor", "label": "Vendor portal only"}]}
    defs = write_defs(tmp_path / "d", MANUAL, files={f"config/{REASONS_NAME}": reasons})
    assert "off_process_reasons" in publish(defs, share, "alice@pc", username="alice").published
    svc = make_services(share, tmp_path / "c.sqlite", registry)
    svc.reasons_source = ReloadingFile(share / "definitions/config" / REASONS_NAME, parse_reasons, DEFAULT_REASONS)
    assert [r.id for r in svc.reasons] == ["vendor"]
    with pytest.raises(CommandError, match="Choose a reason"):
        log(svc, reason="urgent")
    assert svc.reason_label("vendor") == "Vendor portal only" and svc.reason_label("gone") == "gone"
    bad = write_defs(tmp_path / "bad", MANUAL, files={f"config/{REASONS_NAME}": {"reasons": [{"id": "Bad Id"}]}})
    from wft.validation import validate_repo
    assert any("'label' is a required property" in i.message for i in validate_repo(bad).errors)
