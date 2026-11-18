import pytest

from wft import engine
from wft.engine import CommandError
from wft.events import Event

N = 0


def stamp(request_id, defn, new, actor="alice", ts=None):
    global N
    N += 1
    return Event(event_id=f"e{N:04d}", request_id=request_id, seq=new.seq, type=new.type, actor=actor,
                 host="h", app_version="t", definition=defn.key, ts=ts or f"2026-11-18T10:00:{N % 60:02d}.000+00:00",
                 payload=new.payload)


def run(defn, *commands):
    """Apply commands in turn, returning (events, state)."""
    events, state = [], None
    for cmd in commands:
        e = stamp("r1", defn, cmd(state))
        events.append(e)
        state = engine.apply(state, e, defn)
    return events, state


def test_one_step_workflow_completes_when_checklist_ticked_and_step_completed(registry):
    defn = registry.get("demo_manual@1")
    events, state = run(
        defn,
        lambda s: engine.create_request(defn, "Test"),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 0, True),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 1, True),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 2, True),
        lambda s: engine.complete_step(s, defn, "do_the_thing"),
    )
    assert state.status == engine.COMPLETED
    assert state.step("do_the_thing").completed_by == "alice"
    assert engine.replay(events, defn) == state


def test_cannot_complete_step_with_unticked_items(registry):
    defn = registry.get("demo_manual@1")
    _, state = run(defn, lambda s: engine.create_request(defn, "Test"))
    with pytest.raises(CommandError, match="3 checklist item"):
        engine.complete_step(state, defn, "do_the_thing")


def test_untick_and_retick(registry):
    defn = registry.get("demo_manual@1")
    _, state = run(
        defn,
        lambda s: engine.create_request(defn, "Test"),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 0, True),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 0, False),
    )
    assert state.step("do_the_thing").ticked == frozenset()
    with pytest.raises(CommandError):
        engine.set_checklist_item(state, defn, "do_the_thing", 0, False)


def test_dependent_step_becomes_ready_after_its_dependency(two_step):
    defn = two_step.get("two@1")
    _, state = run(defn, lambda s: engine.create_request(defn, "T"))
    assert [s.status for s in state.steps] == ["ready", "pending"]
    with pytest.raises(CommandError, match="pending"):
        engine.complete_step(state, defn, "b")
    _, state = run(
        defn,
        lambda s: engine.create_request(defn, "T"),
        lambda s: engine.set_checklist_item(s, defn, "a", 0, True),
        lambda s: engine.complete_step(s, defn, "a"),
    )
    assert [s.status for s in state.steps] == ["done", "ready"]
    assert state.status == engine.ACTIVE


def test_completed_request_rejects_commands(registry):
    defn = registry.get("demo_manual@1")
    _, state = run(
        defn,
        lambda s: engine.create_request(defn, "Test"),
        *[lambda s, i=i: engine.set_checklist_item(s, defn, "do_the_thing", i, True) for i in range(3)],
        lambda s: engine.complete_step(s, defn, "do_the_thing"),
    )
    with pytest.raises(CommandError, match="Completed"):
        engine.set_checklist_item(state, defn, "do_the_thing", 0, False)


def test_blank_title_rejected(registry):
    with pytest.raises(CommandError):
        engine.create_request(registry.get("demo_manual@1"), "   ")


def test_replay_orders_by_sequence_not_arrival(registry):
    defn = registry.get("demo_manual@1")
    events, state = run(
        defn,
        lambda s: engine.create_request(defn, "Test"),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 0, True),
        lambda s: engine.set_checklist_item(s, defn, "do_the_thing", 1, True),
    )
    assert engine.replay(list(reversed(events)), defn) == state


def test_same_sequence_conflict_keeps_earliest_deterministically(registry):
    defn = registry.get("demo_manual@1")
    events, state = run(defn, lambda s: engine.create_request(defn, "Test"))
    a = stamp("r1", defn, engine.set_checklist_item(state, defn, "do_the_thing", 0, True),
              actor="alice", ts="2026-11-18T11:00:00.000+00:00")
    b = stamp("r1", defn, engine.set_checklist_item(state, defn, "do_the_thing", 1, True),
              actor="bob", ts="2026-11-18T11:00:01.000+00:00")
    for order in ([a, b], [b, a]):
        result = engine.replay(events + order, defn)
        assert result.step("do_the_thing").ticked == {0}
