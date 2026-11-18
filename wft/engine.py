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
    return NewEvent(CHECKLIST_ITEM_SET, seq=state.seq + 1,
                    payload={"step": step_id, "index": index, "checked": checked})


def complete_step(state: RequestState, defn: WorkflowDef, step_id: str) -> NewEvent:
    step = _ready_step(state, defn, step_id)
    missing = len(defn.step(step_id).checklist) - len(step.ticked)
    if missing:
        raise CommandError(f"{missing} checklist item(s) still to tick")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, payload={"step": step_id})


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
            seq=event.seq, steps=steps,
        )
        return _advance(state, defn)

    if state is None:
        raise ValueError(f"{event.request_id}: {event.type} before {REQUEST_CREATED}")
    state = replace(state, seq=event.seq, updated_at=event.ts)

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


def order_events(events: Iterable[Event]) -> list[Event]:
    """Order one request's events by sequence.

    If two events claim the same sequence (two users acted at once), the one
    with the earliest timestamp, then lowest event id, wins and the other is
    dropped. Surfacing the losing action to its author is ticket #3.
    """
    by_seq: dict[int, Event] = {}
    for e in events:
        cur = by_seq.get(e.seq)
        if cur is None or (e.ts, e.event_id) < (cur.ts, cur.event_id):
            by_seq[e.seq] = e
    return [by_seq[k] for k in sorted(by_seq)]


def replay(events: Iterable[Event], defn: WorkflowDef) -> RequestState:
    state: RequestState | None = None
    for e in order_events(events):
        if state is not None and e.seq != state.seq + 1:
            break  # gap: a later event arrived before its predecessor; wait for it
        state = apply(state, e, defn)
    if state is None:
        raise ValueError("No events to replay")
    return state
