"""Hold, resume, cancel, impact flags, final states and follow-ups."""
import pytest

from tests.conftest import DEMO, make_services
from wft import engine, timings
from wft.engine import CommandError

ACC = {"capture": {"account": "ACC1"}}


@pytest.fixture
def svc(share, tmp_path, krm_registry):
    s = make_services(share, tmp_path / "a.sqlite", krm_registry, "alice")
    s._acknowledged_path = tmp_path / "ack-a.json"
    return s


def submitted(svc, title="ACC1 change"):
    rid = svc.create_request("krm@1", title)
    svc.submit(rid, ACC)
    return rid


def raise_done(svc, rid):
    svc.set_checklist_item(rid, "raise", 0, True)
    svc.complete_step(rid, "raise")


def test_hold_needs_a_reason_blocks_work_and_resume_returns_to_active(svc):
    rid = submitted(svc)
    with pytest.raises(CommandError, match="reason"):
        svc.hold(rid, "  ")
    svc.hold(rid, "Client paused")
    state = svc.get(rid)[0]
    assert state.status == engine.ON_HOLD and state.holds[0].reason == "Client paused" and state.holds[0].end is None
    assert state.flags == ()  # "raise" is only ready, not in flight
    with pytest.raises(CommandError, match="OnHold"):
        svc.set_checklist_item(rid, "raise", 0, True)
    with pytest.raises(CommandError, match="Only Active"):
        svc.hold(rid, "again")
    svc.add_note(rid, "Notes still allowed while on hold")
    svc.resume(rid)
    state = svc.get(rid)[0]
    assert state.status == engine.ACTIVE and state.holds[0].end is not None
    with pytest.raises(CommandError, match="isn't on hold"):
        svc.resume(rid)


def test_hold_flags_in_flight_steps_with_on_hold(svc):
    rid = submitted(svc)
    svc.start_step(rid, "raise")
    svc.hold(rid, "Client paused")
    [flag] = svc.get(rid)[0].flags
    assert (flag.step, flag.trigger, flag.action) == ("raise", "hold", "KRM22 may still approve ACC1")
    assert flag.compensation == "Withdraw KRM22 request for ACC1" and flag.raised_by == "alice" and flag.raised_at


def test_open_hold_flag_stops_completion_until_resolved(svc):
    rid = submitted(svc)
    svc.start_step(rid, "raise")
    svc.hold(rid, "paused")
    svc.resume(rid)
    raise_done(svc, rid)
    svc.set_checklist_item(rid, "apply", 0, True)
    svc.complete_step(rid, "apply")
    state = svc.get(rid)[0]
    assert state.status == engine.ACTIVE and state.awaiting_flags and state.completed_at is None
    svc.resolve_flag(rid, state.flags[0].id, "not_required", "KRM22 request went through as planned")
    state = svc.get(rid)[0]
    assert state.status == engine.COMPLETED and state.completed_at


def test_cancel_draft_is_immediate(svc):
    rid = svc.create_request("krm@1", "never mind")
    with pytest.raises(CommandError, match="reason"):
        svc.cancel(rid, "")
    svc.cancel(rid, "Raised by mistake")
    state = svc.get(rid)[0]
    assert state.status == engine.CANCELLED and state.flags == () and state.cancel_reason == "Raised by mistake"


def test_cancel_flags_work_already_done_and_finishes_when_resolved(svc, krm_registry):
    rid = submitted(svc)
    raise_done(svc, rid)            # done: flagged with its on_cancel text
    svc.start_step(rid, "apply")     # started, no on_cancel: generic flag
    svc.cancel(rid, "Client withdrew")
    state = svc.get(rid)[0]
    assert state.status == engine.CANCELLING
    by_step = {f.step: f for f in state.flags}
    assert set(by_step) == {"raise", "apply"}  # the capture form isn't flagged (no on_cancel)
    assert by_step["raise"].action == "Withdraw the ACC1 request in KRM22"
    assert by_step["apply"].action == engine.GENERIC_ROLLBACK and by_step["apply"].compensation is None
    with pytest.raises(CommandError, match="can't be cancelled"):
        svc.cancel(rid, "again")

    svc.resolve_flag(rid, by_step["raise"].id, "deferred", "KRM22 is down; withdraw tomorrow")
    assert svc.get(rid)[0].status == engine.CANCELLING  # "apply" is still open
    svc.resolve_flag(rid, by_step["apply"].id, "actioned", "Nothing had been applied yet")
    state = svc.get(rid)[0]
    assert state.status == engine.CANCELLED and state.cancelled_at  # the deferred flag doesn't block
    assert [o.flag.step for o in svc.open_flags()] == ["raise"]   # ...but stays visible
    svc.resolve_flag(rid, by_step["raise"].id, "actioned", "Withdrawn in KRM22", "KRM-2026-0001")
    flag = svc.get(rid)[0].flag(by_step["raise"].id)
    assert flag.resolution == "actioned" and flag.linked_request == rid and flag.resolved_by == "alice"
    assert svc.open_flags() == []


def test_resolution_rules(svc):
    rid = submitted(svc)
    raise_done(svc, rid)
    svc.cancel(rid, "x")
    fid = svc.get(rid)[0].flags[0].id
    for resolution, message in (("actioned", "Say what was done"), ("not_required", "why it isn't required"),
                                ("deferred", "why it's deferred")):
        with pytest.raises(CommandError, match=message):
            svc.resolve_flag(rid, fid, resolution, " ")
    with pytest.raises(CommandError, match="Choose"):
        svc.resolve_flag(rid, fid, "ignored", "x")
    with pytest.raises(CommandError, match="No request"):
        svc.resolve_flag(rid, fid, "actioned", "x", "NOPE-1")
    svc.resolve_flag(rid, fid, "deferred", "later")
    with pytest.raises(CommandError, match="already deferred"):
        svc.resolve_flag(rid, fid, "deferred", "later again")
    svc.resolve_flag(rid, fid, "not_required", "turned out fine")
    with pytest.raises(CommandError, match="already resolved"):
        svc.resolve_flag(rid, fid, "actioned", "x")


def test_simultaneous_resolutions_first_wins(svc, share, tmp_path, krm_registry):
    bob = make_services(share, tmp_path / "b.sqlite", krm_registry, "bob")
    bob._acknowledged_path = tmp_path / "ack-b.json"
    rid = submitted(svc)
    raise_done(svc, rid)
    svc.cancel(rid, "x")
    bob.sync()
    fid = svc.get(rid)[0].flags[0].id
    svc.resolve_flag(rid, fid, "actioned", "alice withdrew it")
    bob.resolve_flag(rid, fid, "not_required", "bob thinks it's fine")
    svc.sync()
    bob.sync()
    assert svc.get(rid)[0].flag(fid).notes == bob.get(rid)[0].flag(fid).notes == "alice withdrew it"
    assert [e.type for e in bob.my_conflicts()] == [engine.FLAG_RESOLVED] and svc.my_conflicts() == []


def test_finished_requests_cannot_change_but_get_follow_ups(share, tmp_path, registry):
    svc = make_services(share, tmp_path / "c.sqlite", registry)
    rid = svc.create_request(DEMO, "Done one")
    svc.submit(rid, {})
    with pytest.raises(CommandError, match="completed or cancelled"):
        svc.create_follow_up(rid)
    for i in range(3):
        svc.set_checklist_item(rid, "do_the_thing", i, True)
    svc.complete_step(rid, "do_the_thing")
    for attempt in (lambda: svc.hold(rid, "x"), lambda: svc.cancel(rid, "x"),
                    lambda: svc.change_owner(rid, "bob"), lambda: svc.claim_step(rid, "do_the_thing")):
        with pytest.raises(CommandError):
            attempt()
    svc.add_note(rid, "Evidence can still be added")
    ref = svc.get(rid)[0].reference
    follow = svc.create_follow_up(rid)
    state = svc.get(follow)[0]
    assert state.status == engine.DRAFT and state.follow_up_of == rid and state.title == f"Follow-up to {ref}"
    assert [r.request_id for r in svc.follow_ups(rid)] == [follow]


def test_hold_time_is_excluded_from_step_timings(registry):
    from tests.test_timings import Run, at
    r = Run(registry.get(DEMO))
    d = r.defn
    r.do(engine.create_request(d, "t"), at("08:00")).do(engine.submit_request(r.state, d, {}), at("09:00"))
    r.do(engine.start_step(r.state, d, "do_the_thing"), at("09:00"))
    r.do(engine.hold_request(r.state, d, "paused"), at("10:00"))
    r.do(engine.resume_request(r.state, d), at("13:00"))
    for i in range(3):
        r.do(engine.set_checklist_item(r.state, d, "do_the_thing", i, True), at("13:30"))
    r.do(engine.complete_step(r.state, d, "do_the_thing"), at("14:00"))
    t = timings.step_timing(r.state.step("do_the_thing"), holds=r.state.holds)
    assert t.held == 3 * 3600 and t.working == 2 * 3600 and t.elapsed == 2 * 3600
    assert timings.time_on_hold(r.state) == 3 * 3600


def test_views_list_flags_and_flagged_requests(svc):
    rid = submitted(svc)
    raise_done(svc, rid)
    svc.cancel(rid, "x")
    other = submitted(svc, "on hold one")
    svc.hold(other, "waiting for budget")
    assert [o.request.request_id for o in svc.open_flags()] == [rid]
    assert [r.request_id for r in svc.tasks().flagged] == [rid]
    titles = lambda st: {r.request.title for r in svc.open_requests(st)}  # noqa: E731
    assert titles("open") == {"ACC1 change", "on hold one"}
    assert titles("on_hold") == {"on hold one"} and titles("cancelling") == {"ACC1 change"}
    assert all(w == [] for w in svc.waiting_board().values())
