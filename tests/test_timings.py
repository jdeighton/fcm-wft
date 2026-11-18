"""Step states, transitions and the timings derived from them."""
from datetime import datetime, timezone

import pytest

from tests.conftest import DEMO, make_services
from wft import engine, timings
from wft.engine import CommandError
from wft.events import Event

STEP = "do_the_thing"
N = iter(range(10**6))


def at(hhmm: str) -> str:
    return f"2026-11-18T{hhmm}:00.000+00:00"


class Run:
    """Drive the engine directly with chosen timestamps."""

    def __init__(self, defn):
        self.defn, self.state, self.events = defn, None, []

    def do(self, new, ts, actor="alice"):
        e = Event(event_id=f"e{next(N):06d}", request_id="r", seq=new.seq, type=new.type, actor=actor, host="h",
                  app_version="t", definition=self.defn.key, ts=ts, payload=new.payload, prev=new.prev)
        self.events.append(e)
        self.state = engine.apply(self.state, e, self.defn)
        return self

    def step(self):
        return self.state.step(STEP)


@pytest.fixture
def run(registry):
    r = Run(registry.get(DEMO))
    r.do(engine.create_request(r.defn, "t"), at("08:00"))
    r.do(engine.submit_request(r.state, r.defn, {}), at("09:00"))
    return r


def test_lifecycle_of_a_step_and_its_timings(run):
    d = run.defn
    assert run.step().status == engine.READY
    run.do(engine.start_step(run.state, d, STEP), at("09:30"))
    assert run.step().status == engine.IN_PROGRESS
    run.do(engine.wait_step(run.state, d, STEP, "client", "signed form"), at("10:00"))
    assert (run.step().status, run.step().wait_reason, run.step().wait_note) == ("waiting", "client", "signed form")
    with pytest.raises(CommandError, match="waiting on client; resume"):
        engine.set_checklist_item(run.state, d, STEP, 0, True)
    run.do(engine.resume_step(run.state, d, STEP), at("12:00"))
    run.do(engine.wait_step(run.state, d, STEP, "krm22"), at("12:15"))
    run.do(engine.resume_step(run.state, d, STEP), at("12:45"), actor="bob")
    for i in range(3):
        run.do(engine.set_checklist_item(run.state, d, STEP, i, True), at("13:00"))
    run.do(engine.complete_step(run.state, d, STEP), at("13:30"))

    s = run.step()
    assert [(t.status, t.ts[11:16], t.actor) for t in s.transitions] == [
        ("ready", "09:00", "alice"), ("in_progress", "09:30", "alice"), ("waiting", "10:00", "alice"),
        ("in_progress", "12:00", "alice"), ("waiting", "12:15", "alice"), ("in_progress", "12:45", "bob"),
        ("done", "13:30", "alice")]
    t = timings.step_timing(s)
    assert (t.ready_at, t.started_at, t.finished_at) == (at("09:00"), at("09:30"), at("13:30"))
    assert t.queued == 30 * 60
    assert t.working == (30 + 15 + 45) * 60
    assert t.waiting == {"client": 2 * 3600, "krm22": 30 * 60}
    assert t.elapsed == 4.5 * 3600 == t.queued + t.working + t.waiting_total
    assert run.state.status == engine.COMPLETED and run.state.completed_at == at("13:30")
    assert timings.request_age(run.state) == 4.5 * 3600  # submitted -> completed


def test_first_work_starts_a_ready_step(run):
    run.do(engine.set_checklist_item(run.state, run.defn, STEP, 0, True), at("09:10"))
    assert run.step().status == engine.IN_PROGRESS and run.step().transitions[-1].ts == at("09:10")
    with pytest.raises(CommandError, match="already started"):
        engine.start_step(run.state, run.defn, STEP)


def test_open_step_timing_runs_to_now(run):
    run.do(engine.wait_step(run.state, run.defn, STEP, "internal"), at("09:20"))
    now = datetime(2026, 11, 18, 11, 20, tzinfo=timezone.utc)
    t = timings.step_timing(run.step(), now)
    assert t.waiting == {"internal": 2 * 3600} and t.queued == 20 * 60 and t.finished_at is None
    assert t.elapsed == (2 * 60 + 20) * 60
    assert timings.time_in_current_step(run.state, now) == 2 * 3600
    assert timings.request_age(run.state, now) == (2 * 60 + 20) * 60


def test_wait_reasons_are_checked(run):
    with pytest.raises(CommandError, match="Choose what"):
        engine.wait_step(run.state, run.defn, STEP, "lunch")
    with pytest.raises(CommandError, match="Say what"):
        engine.wait_step(run.state, run.defn, STEP, "other", "  ")
    with pytest.raises(CommandError, match="isn't waiting"):
        engine.resume_step(run.state, run.defn, STEP)


def test_skipped_counts_as_finished_for_dependencies(two_step):
    defn = two_step.get("two@1")
    r = Run(defn)
    r.do(engine.create_request(defn, "t"), at("08:00")).do(engine.submit_request(r.state, defn, {}), at("08:00"))
    # Simulate a step being skipped (decision steps will do this): b becomes ready, request not complete.
    a = engine._to(r.state.step("a"), engine.SKIPPED, r.events[-1])
    r.state = engine._advance(engine._replace_step(r.state, a), defn, r.events[-1])
    # b depends only on a skipped step, so it's skipped too (a branch not taken) and the request completes.
    assert r.state.step("b").status == engine.SKIPPED and r.state.status == engine.COMPLETED
    assert timings.step_timing(r.state.step("a")).finished_at == at("08:00")


@pytest.mark.parametrize("seconds, text", [
    (None, "—"), (5, "<1m"), (59, "<1m"), (60, "1m"), (59 * 60, "59m"), (3600, "1h 00m"),
    (3600 * 3 + 300, "3h 05m"), (86400 * 2 + 3600 * 4, "2d 4h"),
])
def test_format_duration(seconds, text):
    assert timings.format_duration(seconds) == text


def test_open_requests_filter_and_sort(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    first = alice.create_request(DEMO, "Alpha limits")
    alice.submit(first, {})
    second = alice.create_request(DEMO, "Bravo user")
    alice.submit(second, {})
    alice.change_owner(second, "bob")
    draft = alice.create_request(DEMO, "Charlie draft")
    done = alice.create_request(DEMO, "Delta done")
    alice.submit(done, {})
    for i in range(3):
        alice.set_checklist_item(done, STEP, i, True)
    alice.complete_step(done, STEP)

    titles = lambda rows: [r.request.title for r in rows]  # noqa: E731
    assert set(titles(alice.open_requests())) == {"Alpha limits", "Bravo user", "Charlie draft"}
    assert titles(alice.open_requests("completed")) == ["Delta done"]
    assert titles(alice.open_requests("draft")) == ["Charlie draft"]
    assert len(alice.open_requests("all")) == 4
    assert titles(alice.open_requests(owner="bob")) == ["Bravo user"]
    assert titles(alice.open_requests(q="bravo")) == ["Bravo user"]
    ref = alice.get(first)[0].reference
    assert titles(alice.open_requests(q=ref.lower())) == ["Alpha limits"]
    assert titles(alice.open_requests("active", sort="title", descending=False)) == ["Alpha limits", "Bravo user"]
    assert titles(alice.open_requests("active", sort="age")) == ["Alpha limits", "Bravo user"]  # oldest first when desc
    assert titles(alice.open_requests(workflow="nope")) == []
    row = next(r for r in alice.open_requests() if r.request.title == "Alpha limits")
    assert row.current_step_title == "Do the thing" and row.current_step_status == "ready" and row.in_step >= 0
    assert draft


def test_waiting_board_groups_by_reason(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    ids = []
    for title, reason in [("one", "client"), ("two", "client"), ("three", "krm22")]:
        rid = alice.create_request(DEMO, title)
        alice.submit(rid, {})
        alice.wait_step(rid, STEP, reason, f"note {title}")
        ids.append(rid)
    board = alice.waiting_board(now=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert list(board) == list(engine.WAIT_REASONS)
    assert [w.request.title for w in board["client"]] == ["one", "two"]  # longest wait first
    assert [(w.request.title, w.note, w.by) for w in board["krm22"]] == [("three", "note three", "alice")]
    assert board["internal"] == []
    alice.resume_step(ids[0], STEP)
    assert [w.request.title for w in alice.waiting_board()["client"]] == ["two"]
    # Waiting steps are still "with" their assignee and show in My tasks.
    alice.claim_step(ids[1], STEP)
    assert [t.request.title for t in alice.tasks().mine] == ["two"]
