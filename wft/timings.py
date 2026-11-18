"""Step and request timings derived from step transitions. Pure functions; all times are UTC.

For each step instance:
- queued:  ready -> first started (time nobody was working on it yet)
- working: time in progress
- waiting: time waiting, per reason
- elapsed: ready -> finished (or now, if still open)
Business-hours calendars are out of scope for now: these are wall-clock durations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from wft.engine import DONE, DONE_OUTSIDE, IN_PROGRESS, READY, SKIPPED, WAITING, Hold, RequestState, StepState


def parse_ts(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def seconds_between(start: str | datetime, end: str | datetime) -> float:
    a = parse_ts(start) if isinstance(start, str) else start
    b = parse_ts(end) if isinstance(end, str) else end
    return max(0.0, (b - a).total_seconds())


@dataclass(frozen=True)
class StepTiming:
    ready_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    queued: float = 0.0
    working: float = 0.0
    waiting: dict[str, float] = field(default_factory=dict)
    elapsed: float | None = None
    held: float = 0.0  # time the request was on hold while this step was open; excluded from the other figures

    @property
    def waiting_total(self) -> float:
        return sum(self.waiting.values())


def _overlap(start: str, end: str | datetime, holds: tuple[Hold, ...], now: datetime) -> float:
    """Seconds of [start, end) during which the request was on hold."""
    a = parse_ts(start)
    b = parse_ts(end) if isinstance(end, str) else end
    total = 0.0
    for h in holds:
        hs, he = parse_ts(h.start), parse_ts(h.end) if h.end else now
        total += max(0.0, (min(b, he) - max(a, hs)).total_seconds())
    return total


def step_timing(step: StepState, now: datetime | None = None, holds: tuple[Hold, ...] = ()) -> StepTiming:
    """Timing for one step from its transitions. Open states run up to `now`; time on hold is set aside."""
    now = now or now_utc()
    if not step.transitions:
        return StepTiming()
    queued = working = held = 0.0
    waiting: dict[str, float] = {}
    ready_at = started_at = finished_at = None
    for i, t in enumerate(step.transitions):
        end = step.transitions[i + 1].ts if i + 1 < len(step.transitions) else None
        if t.status == READY and ready_at is None:
            ready_at = t.ts
        if t.status in (IN_PROGRESS, WAITING, DONE, DONE_OUTSIDE) and started_at is None:
            started_at = t.ts
        if t.status in (DONE, DONE_OUTSIDE, SKIPPED):
            finished_at = t.ts
            continue
        finished_at = None  # reopened (e.g. reset by an amendment): it isn't finished any more
        on_hold = _overlap(t.ts, end or now, holds, now)
        held += on_hold
        span = seconds_between(t.ts, end or now) - on_hold
        if t.status == READY:
            queued += span
        elif t.status == IN_PROGRESS:
            working += span
        elif t.status == WAITING:
            waiting[t.reason or "other"] = waiting.get(t.reason or "other", 0.0) + span
    begin = ready_at or step.transitions[0].ts
    elapsed = seconds_between(begin, finished_at or now) - held
    return StepTiming(ready_at, started_at, finished_at, queued, working, waiting, elapsed, held)


def time_on_hold(state: RequestState, now: datetime | None = None) -> float:
    now = now or now_utc()
    return sum(seconds_between(h.start, h.end or now) for h in state.holds)


def request_age(state: RequestState, now: datetime | None = None) -> float:
    """Since the request was submitted (or created, for a Draft) until it completed, or now."""
    end = state.completed_at or state.cancelled_at or now or now_utc()
    return seconds_between(state.submitted_at or state.created_at, end)


def time_in_current_step(state: RequestState, now: datetime | None = None) -> float | None:
    if state.status == "Draft":
        return seconds_between(state.created_at, now or now_utc())
    step = next((s for s in state.steps if s.id == state.current_step), None)
    if step is None or step.since is None:
        return None
    return seconds_between(step.since, now or now_utc())


def format_duration(seconds: float | None) -> str:
    """Compact human duration: '45s' is shown as '<1m'; then '12m', '3h 05m', '2d 4h'."""
    if seconds is None:
        return "—"
    minutes = int(seconds // 60)
    if minutes < 1:
        return "<1m"
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"
