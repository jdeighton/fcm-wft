import pytest

from tests.conftest import DEMO, make_services
from wft.engine import COMPLETED, CommandError


def complete_demo(svc, request_id):
    for i in range(3):
        svc.set_checklist_item(request_id, "do_the_thing", i, True)
    svc.complete_step(request_id, "do_the_thing")


def test_create_and_complete_request(services):
    rid = services.create_request(DEMO, "Update limits for ACC1")
    services.submit(rid, {})
    complete_demo(services, rid)
    state, _ = services.get(rid)
    assert state.status == COMPLETED
    assert [e.type for e in services.rm.history(rid)] == [
        "request_created", "request_submitted", "reference_assigned",
        "checklist_item_set", "checklist_item_set", "checklist_item_set", "step_completed"]
    assert all(e.actor == "alice" and e.definition == DEMO for e in services.rm.history(rid))
    assert state.reference.startswith("REQ-")


def test_deleting_cache_rebuilds_identical_state(share, tmp_path, registry):
    cache = tmp_path / "cache" / "uat.sqlite"
    svc = make_services(share, cache, registry)
    a = svc.create_request(DEMO, "A")
    svc.submit(a, {})
    complete_demo(svc, a)
    b = svc.create_request(DEMO, "B")
    svc.submit(b, {})
    svc.set_checklist_item(b, "do_the_thing", 1, True)
    before = svc.rm.dump()
    svc.rm.close()

    cache.unlink()
    rebuilt = make_services(share, cache, registry)
    assert rebuilt.rm.dump() == before
    assert len(before) == 2


def test_another_users_requests_appear_after_sync(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    bob = make_services(share, tmp_path / "b.sqlite", registry, "bob")
    rid = alice.create_request(DEMO, "From Alice")
    alice.submit(rid, {})
    bob.sync()
    bob.set_checklist_item(rid, "do_the_thing", 0, True)
    alice.sync()
    state, _ = alice.get(rid)
    assert state.step("do_the_thing").ticked == {0}
    assert {e.actor for e in alice.rm.history(rid)} == {"alice", "bob"}


def test_unknown_workflow_rejected(services):
    with pytest.raises(CommandError):
        services.create_request("nope@1", "x")
