"""Pure workflow engine: (definition, events) -> state, and commands -> new events.

No I/O, clocks or ids here. Callers stamp the envelope (ids, actor, time) and
persist events; the engine only decides what may happen and folds events into state.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable

from wft.definitions import WorkflowDef
from wft.events import Event

REQUEST_CREATED = "request_created"
CHECKLIST_ITEM_SET = "checklist_item_set"
STEP_COMPLETED = "step_completed"

ACTIVE = "Active"
COMPLETED = "Completed"

PENDING = "pending"
READY = "ready"
DONE = "done"


class CommandError(Exception):
    """A command is not valid for the current state."""


@dataclass(frozen=True)
class NewEvent:
    """An event decided by the engine, before the envelope is stamped."""
    type: str
    seq: int
    payload: dict[str, Any] = field(default_factory=dict)
    prev: str | None = None


@dataclass(frozen=True)
class StepState:
    id: str
    status: str
    ticked: frozenset[int] = frozenset()
    completed_by: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class RequestState:
    request_id: str
    workflow: str  # "<id>@<version>"
    title: str
    status: str
    created_by: str
    created_at: str
    updated_at: str
    seq: int
    last_event: str
    steps: tuple[StepState, ...]

    def step(self, step_id: str) -> StepState:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @property
    def current_step(self) -> str | None:
        return next((s.id for s in self.steps if s.status == READY), None)


# --- commands -------------------------------------------------------------

def create_request(defn: WorkflowDef, title: str) -> NewEvent:
    title = title.strip()
    if not title:
        raise CommandError("A title is required")
    return NewEvent(REQUEST_CREATED, seq=1, payload={"title": title})


def set_checklist_item(state: RequestState, defn: WorkflowDef, step_id: str, index: int, checked: bool) -> NewEvent:
    step = _ready_step(state, defn, step_id)
    if not 0 <= index < len(defn.step(step_id).checklist):
        raise CommandError(f"Step {step_id!r} has no checklist item {index}")
    if (index in step.ticked) == checked:
        raise CommandError("Checklist item is already in that state")
    return NewEvent(CHECKLIST_ITEM_SET, seq=state.seq + 1, prev=state.last_event,
                    payload={"step": step_id, "index": index, "checked": checked})


def complete_step(state: RequestState, defn: WorkflowDef, step_id: str) -> NewEvent:
    step = _ready_step(state, defn, step_id)
    missing = len(defn.step(step_id).checklist) - len(step.ticked)
    if missing:
        raise CommandError(f"{missing} checklist item(s) still to tick")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id})


def _ready_step(state: RequestState, defn: WorkflowDef, step_id: str) -> StepState:
    if state.status != ACTIVE:
        raise CommandError(f"Request is {state.status}")
    try:
        step = state.step(step_id)
    except KeyError:
        raise CommandError(f"Unknown step {step_id!r}") from None
    if step.status != READY:
        raise CommandError(f"Step {step_id!r} is {step.status}")
    return step


# --- folding events into state -------------------------------------------

def apply(state: RequestState | None, event: Event, defn: WorkflowDef) -> RequestState:
    if event.type == REQUEST_CREATED:
        if state is not None:
            raise ValueError(f"{event.request_id}: duplicate {REQUEST_CREATED}")
        steps = tuple(StepState(s.id, PENDING) for s in defn.steps)
        state = RequestState(
            request_id=event.request_id, workflow=defn.key, title=event.payload["title"],
            status=ACTIVE, created_by=event.actor, created_at=event.ts, updated_at=event.ts,
            seq=event.seq, last_event=event.event_id, steps=steps,
        )
        return _advance(state, defn)

    if state is None:
        raise ValueError(f"{event.request_id}: {event.type} before {REQUEST_CREATED}")
    state = replace(state, seq=event.seq, last_event=event.event_id, updated_at=event.ts)

    if event.type == CHECKLIST_ITEM_SET:
        step = state.step(event.payload["step"])
        idx = event.payload["index"]
        ticked = step.ticked | {idx} if event.payload["checked"] else step.ticked - {idx}
        return _replace_step(state, replace(step, ticked=frozenset(ticked)))

    if event.type == STEP_COMPLETED:
        step = state.step(event.payload["step"])
        state = _replace_step(state, replace(step, status=DONE, completed_by=event.actor, completed_at=event.ts))
        return _advance(state, defn)

    raise ValueError(f"Unknown event type {event.type!r}")


def _replace_step(state: RequestState, new: StepState) -> RequestState:
    return replace(state, steps=tuple(new if s.id == new.id else s for s in state.steps))


def _advance(state: RequestState, defn: WorkflowDef) -> RequestState:
    """Make pending steps ready once their dependencies are done; complete the request when all are done."""
    done = {s.id for s in state.steps if s.status == DONE}
    steps = tuple(
        replace(s, status=READY)
        if s.status == PENDING and set(defn.step(s.id).depends_on) <= done else s
        for s in state.steps
    )
    status = COMPLETED if all(s.status == DONE for s in steps) else state.status
    return replace(state, steps=steps, status=status)


@dataclass(frozen=True)
class Replay:
    state: RequestState
    losers: tuple[Event, ...]  # events overridden by a concurrent action, oldest first


def resolve(events: Iterable[Event]) -> tuple[list[Event], list[Event]]:
    """Split one request's events into the winning chain and the losers.

    Order is by per-request sequence, never wall-clock. When two users act at
    once, both events claim the same sequence; the one with the earliest
    timestamp (then lowest event id) wins. Any event decided on top of a loser
    (its `prev` is a loser) also loses. Events after a gap in the sequence are
    neither: they wait until their predecessor has been read.
    """
    by_seq: dict[int, list[Event]] = {}
    for e in events:
        by_seq.setdefault(e.seq, []).append(e)

    chain: list[Event] = []
    losers: list[Event] = []
    lost: set[str] = set()
    stalled = False
    for seq in sorted(by_seq):
        candidates = sorted(by_seq[seq], key=lambda e: (e.ts, e.event_id))
        winner = None
        if not stalled and seq == len(chain) + 1:
            head = chain[-1].event_id if chain else None
            # prev=None on a later event only occurs in journals written before prev existed.
            winner = next((e for e in candidates
                           if e.prev not in lost and (e.prev == head or e.prev is None)), None)
        if winner is None:
            stalled = True
        else:
            chain.append(winner)
        for e in candidates:
            if e is not winner and (winner is not None or e.prev in lost):
                lost.add(e.event_id)
                losers.append(e)
    return chain, losers


def replay(events: Iterable[Event], defn: WorkflowDef) -> Replay:
    chain, losers = resolve(events)
    state: RequestState | None = None
    for e in chain:
        state = apply(state, e, defn)
    if state is None:
        raise ValueError("No events to replay")
    return Replay(state, tuple(losers))
