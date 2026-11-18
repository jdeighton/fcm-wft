"""Draft -> submit, form steps, notes and readable references."""
import threading

import pytest

from tests.conftest import make_services
from wft import engine
from wft.engine import FormInvalid
from wft.repository import ReferenceAllocator, ReferenceUnavailable
from wft.services import Services

CAPTURE = {"account": "ACC1", "limits": {"max": 10}}


@pytest.fixture
def svc(share, tmp_path, forms_registry) -> Services:
    return make_services(share, tmp_path / "c.sqlite", forms_registry)


def test_request_starts_as_draft_and_keeps_saved_form_data(svc):
    rid = svc.create_request("limits@1", "ACC1 limits")
    state, _ = svc.get(rid)
    assert state.status == engine.DRAFT and state.reference is None
    assert all(s.status == engine.PENDING for s in state.steps)
    svc.save_form(rid, "capture", {"account": "AC"})  # partial: not validated
    assert svc.get(rid)[0].step("capture").draft == {"account": "AC"}
    with pytest.raises(engine.CommandError, match="until the request is submitted"):
        svc.save_form(rid, "record", {})
    with pytest.raises(engine.CommandError, match="Draft"):
        svc.set_checklist_item(rid, "apply", 0, True)


def test_submit_validates_then_activates_with_inputs_and_reference(svc):
    rid = svc.create_request("limits@1", "ACC1 limits")
    with pytest.raises(FormInvalid) as exc:
        svc.submit(rid, {"capture": {"account": "ACC1"}})
    assert exc.value.step_id == "capture" and exc.value.errors == {"limits": "Required"}
    assert svc.get(rid)[0].status == engine.DRAFT

    svc.submit(rid, {"capture": CAPTURE})
    state, _ = svc.get(rid)
    assert state.status == engine.ACTIVE and state.inputs == CAPTURE
    assert state.step("capture").status == engine.DONE and state.step("capture").data == CAPTURE
    assert state.current_step == "apply"
    assert state.reference == f"LIM-{state.submitted_at[:4]}-0001"
    with pytest.raises(engine.CommandError, match="not Draft"):
        svc.submit(rid, {"capture": CAPTURE})


def test_later_form_step_is_validated_and_completes_the_request(svc):
    rid = svc.create_request("limits@1", "x")
    svc.submit(rid, {"capture": CAPTURE})
    svc.set_checklist_item(rid, "apply", 0, True)
    svc.complete_step(rid, "apply")
    with pytest.raises(engine.CommandError, match="submit the form"):
        svc.complete_step(rid, "record")
    svc.save_form(rid, "record", {"ticket": "K"})
    with pytest.raises(FormInvalid):
        svc.complete_form(rid, "record", {})
    svc.complete_form(rid, "record", {"ticket": "K-42"})
    state, _ = svc.get(rid)
    assert state.status == engine.COMPLETED and state.step("record").data == {"ticket": "K-42"}


def test_references_are_sequential_per_prefix(svc, share, tmp_path, registry):
    refs = []
    for i in range(3):
        rid = svc.create_request("limits@1", f"r{i}")
        svc.submit(rid, {"capture": CAPTURE})
        refs.append(svc.get(rid)[0].reference)
    year = refs[0][4:8]
    assert refs == [f"LIM-{year}-0001", f"LIM-{year}-0002", f"LIM-{year}-0003"]
    other = make_services(share, tmp_path / "o.sqlite", registry, "bob")
    rid = other.create_request("demo_manual@1", "demo")
    other.submit(rid, {})
    assert other.get(rid)[0].reference == f"REQ-{year}-0001"


def test_concurrent_allocation_never_duplicates(share):
    alloc = ReferenceAllocator(share, wait=10)
    got, errors = [], []

    def take():
        try:
            for _ in range(10):
                got.append(alloc.allocate("LIM", 2026))
        except Exception as e:  # pragma: no cover
            errors.append(e)
    threads = [threading.Thread(target=take) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert sorted(got) == [f"LIM-2026-{n:04d}" for n in range(1, 41)]


def test_reference_allocated_later_when_lock_is_busy(svc, share):
    svc.references = ReferenceAllocator(share, wait=0.1)
    year_lock = None
    rid = svc.create_request("limits@1", "x")
    # Someone else is holding the counter lock right now.
    svc.save_form(rid, "capture", CAPTURE)
    import datetime
    year = datetime.datetime.now(datetime.timezone.utc).year
    year_lock = share / "locks" / f"reference-LIM-{year}.lock"
    year_lock.parent.mkdir(parents=True, exist_ok=True)
    year_lock.write_text("busy")
    svc.submit(rid, {"capture": CAPTURE})
    state, _ = svc.get(rid)
    assert state.status == engine.ACTIVE and state.reference is None and state.request_id == rid
    svc.sync()
    assert svc.get(rid)[0].reference is None  # still busy
    year_lock.unlink()
    svc.sync()
    assert svc.get(rid)[0].reference == f"LIM-{year}-0001"


def test_stale_reference_lock_is_taken_over(share):
    import os
    import time
    lock = share / "locks" / "reference-LIM-2026.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("crashed")
    old = time.time() - 120
    os.utime(lock, (old, old))
    assert ReferenceAllocator(share, wait=0.1).allocate("LIM", 2026) == "LIM-2026-0001"


def test_unreachable_share_raises_reference_unavailable(tmp_path):
    with pytest.raises(ReferenceUnavailable):
        ReferenceAllocator(tmp_path / "gone").allocate("LIM", 2026)


def test_only_the_submitter_assigns_the_reference(svc, share, tmp_path, forms_registry):
    bob = make_services(share, tmp_path / "b.sqlite", forms_registry, "bob")
    svc.references = None  # alice can't allocate right now
    rid = svc.create_request("limits@1", "x")
    svc.submit(rid, {"capture": CAPTURE})
    bob.sync()
    assert bob.get(rid)[0].reference is None  # bob doesn't allocate alice's
    svc.references = ReferenceAllocator(share)
    svc.sync()
    bob.sync()
    assert bob.get(rid)[0].reference.startswith("LIM-")


def test_notes_appear_in_history_and_never_conflict(svc, share, tmp_path, forms_registry):
    bob = make_services(share, tmp_path / "b.sqlite", forms_registry, "bob")
    rid = svc.create_request("limits@1", "x")
    svc.submit(rid, {"capture": CAPTURE})
    bob.sync()
    # Alice ticks while Bob adds a note on the same stale view: both stand.
    svc.set_checklist_item(rid, "apply", 0, True)
    bob.add_note(rid, "Client called to confirm")
    svc.add_note(rid, "  Waiting on KRM22  ")
    svc.sync()
    bob.sync()
    state, _ = svc.get(rid)
    assert [(n.actor, n.text) for n in state.notes] == [("bob", "Client called to confirm"), ("alice", "Waiting on KRM22")]
    assert state.step("apply").ticked == {0}
    assert svc.my_conflicts() == [] and bob.my_conflicts() == []
    types = [e.type for e in svc.rm.history(rid)]
    assert types.count("note_added") == 2 and "reference_assigned" in types
    with pytest.raises(engine.CommandError, match="empty"):
        svc.add_note(rid, "   ")


def test_dry_run_uses_samples_for_forms(forms_registry):
    from wft.validation import dry_run
    defn = forms_registry.get("limits@1")
    assert dry_run(defn, {"capture": CAPTURE, "record": {"ticket": "K"}}) == []
    problems = dry_run(defn, {"capture": {}})
    assert problems and "submit" in problems[0]


def test_definitions_with_forms_validate(tmp_path):
    from tests.defs import FORMS, FORMS_FILES, write_defs
    from wft.validation import validate_repo
    report = validate_repo(write_defs(tmp_path / "d", FORMS, files=FORMS_FILES))
    assert report.ok, [str(i) for i in report.issues]
