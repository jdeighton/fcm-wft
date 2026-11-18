"""Pure workflow engine: (definition, events) -> state, and commands -> new events.

No I/O, clocks or ids here. Callers stamp the envelope (ids, actor, time) and
persist events; the engine only decides what may happen and folds events into state.

Most events form a per-request chain ordered by `seq` (see `resolve`). A few
*annotation* events (references, notes) have `seq = 0`: they sit outside the
chain, so they can never conflict with another user's action.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

import jsonschema

from wft.definitions import StepDef, WorkflowDef
from wft.events import Event

REQUEST_CREATED = "request_created"
REQUEST_SUBMITTED = "request_submitted"
FORM_SAVED = "form_saved"
CHECKLIST_ITEM_SET = "checklist_item_set"
STEP_COMPLETED = "step_completed"
REFERENCE_ASSIGNED = "reference_assigned"
NOTE_ADDED = "note_added"
ANNOTATIONS = frozenset({REFERENCE_ASSIGNED, NOTE_ADDED})
ANNOTATION_SEQ = 0

# Step types this version of the engine can run. Definitions using others fail validation.
SUPPORTED_STEP_TYPES = frozenset({"manual", "form"})

DRAFT = "Draft"
ACTIVE = "Active"
COMPLETED = "Completed"

PENDING = "pending"
READY = "ready"
DONE = "done"

MAX_NOTE_LENGTH = 4000


class CommandError(Exception):
    """A command is not valid for the current state."""


class FormInvalid(CommandError):
    """Submitted form data doesn't satisfy the step's schema."""

    def __init__(self, step_id: str, errors: dict[str, str]):
        super().__init__(f"{len(errors)} field(s) in {step_id!r} need attention")
        self.step_id = step_id
        self.errors = errors


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
    data: dict | None = None    # submitted form data
    draft: dict | None = None   # form data saved but not yet submitted


@dataclass(frozen=True)
class Note:
    ts: str
    actor: str
    text: str


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
    inputs: dict = field(default_factory=dict)
    submitted_by: str | None = None
    submitted_at: str | None = None
    reference: str | None = None
    notes: tuple[Note, ...] = ()

    def step(self, step_id: str) -> StepState:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @property
    def current_step(self) -> str | None:
        return next((s.id for s in self.steps if s.status == READY), None)


def initial_forms(defn: WorkflowDef) -> list[StepDef]:
    """Form steps with no dependencies: filled in while the request is a Draft; their data is the request's inputs."""
    return [s for s in defn.steps if s.type == "form" and not s.depends_on]


# --- form validation -------------------------------------------------------

def validate_form(schema: dict, data: Any) -> dict[str, str]:
    """Field path (dotted, e.g. `limits.max`, `accounts.0.id`) -> message. Empty when valid."""
    errors: dict[str, str] = {}
    for e in sorted(jsonschema.Draft202012Validator(schema).iter_errors(data), key=lambda e: list(e.absolute_path)):
        base = [str(p) for p in e.absolute_path]
        if e.validator == "required":
            for name in e.validator_value:
                if isinstance(e.instance, dict) and name not in e.instance:
                    errors.setdefault(".".join(base + [name]), "Required")
            continue
        errors.setdefault(".".join(base) or "_form", _friendly(e))
    return errors


def _friendly(e: jsonschema.ValidationError) -> str:
    v, val = e.validator, e.validator_value
    if v == "enum":
        return "Choose one of: " + ", ".join(str(x) for x in val)
    if v == "type":
        return {"number": "Enter a number", "integer": "Enter a whole number", "boolean": "Tick or untick",
                "string": "Enter text", "array": "Enter a list", "object": "Fill in this section"}.get(val, e.message)
    if v == "minimum":
        return f"Must be at least {val}"
    if v == "maximum":
        return f"Must be at most {val}"
    if v == "exclusiveMinimum":
        return f"Must be more than {val}"
    if v == "minLength":
        return "Required" if val == 1 else f"Must be at least {val} characters"
    if v == "maxLength":
        return f"Must be at most {val} characters"
    if v == "minItems":
        return "Add at least one" if val == 1 else f"Add at least {val}"
    if v == "pattern":
        return e.schema.get("x-pattern-message") or "Not in the expected format"
    if v == "format":
        return f"Not a valid {val}"
    return re.sub(r"^.*? is not ", "Not ", e.message)


# --- commands -------------------------------------------------------------

def create_request(defn: WorkflowDef, title: str) -> NewEvent:
    title = title.strip()
    if not title:
        raise CommandError("A title is required")
    return NewEvent(REQUEST_CREATED, seq=1, payload={"title": title})


def save_form(state: RequestState, defn: WorkflowDef, step_id: str, data: dict) -> NewEvent:
    """Save form data without validating it, to finish later."""
    _form_step(state, defn, step_id)
    return NewEvent(FORM_SAVED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id, "data": data})


def submit_request(state: RequestState, defn: WorkflowDef, forms: dict[str, dict]) -> NewEvent:
    """Validate the initial form(s) and move the request from Draft to Active."""
    if state.status != DRAFT:
        raise CommandError(f"Request is {state.status}, not {DRAFT}")
    data = {}
    for sd in initial_forms(defn):
        values = forms.get(sd.id, {})
        errors = validate_form(sd.form_schema or {}, values)
        if errors:
            raise FormInvalid(sd.id, errors)
        data[sd.id] = values
    return NewEvent(REQUEST_SUBMITTED, seq=state.seq + 1, prev=state.last_event, payload={"forms": data})


def complete_form(state: RequestState, defn: WorkflowDef, step_id: str, data: dict) -> NewEvent:
    """Validate and submit a form step that became ready while the request is Active."""
    step = _ready_step(state, defn, step_id)
    sd = defn.step(step_id)
    if sd.type != "form":
        raise CommandError(f"Step {step_id!r} is not a form")
    errors = validate_form(sd.form_schema or {}, data)
    if errors:
        raise FormInvalid(step_id, errors)
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event,
                    payload={"step": step.id, "data": data})


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
    if defn.step(step_id).type == "form":
        raise CommandError(f"Step {step_id!r} is a form; submit the form to complete it")
    missing = len(defn.step(step_id).checklist) - len(step.ticked)
    if missing:
        raise CommandError(f"{missing} checklist item(s) still to tick")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id})


def assign_reference(state: RequestState, reference: str) -> NewEvent:
    if state.status == DRAFT:
        raise CommandError("References are assigned when a request is submitted")
    if state.reference:
        raise CommandError(f"Request already has reference {state.reference}")
    return NewEvent(REFERENCE_ASSIGNED, seq=ANNOTATION_SEQ, payload={"reference": reference})


def add_note(state: RequestState, text: str) -> NewEvent:
    text = text.strip()
    if not text:
        raise CommandError("A note can't be empty")
    if len(text) > MAX_NOTE_LENGTH:
        raise CommandError(f"Notes are limited to {MAX_NOTE_LENGTH} characters")
    return NewEvent(NOTE_ADDED, seq=ANNOTATION_SEQ, payload={"text": text})


Command = Callable[[RequestState], NewEvent]


def actions(state: RequestState, defn: WorkflowDef,
            samples: dict[str, dict] | None = None) -> list[tuple[str, list[Command]]]:
    """The distinct things a user could do next, each as the commands that do it.

    Used by the validator's dry run to walk every path through a workflow;
    `samples` supplies form data per form step.
    """
    samples = samples or {}
    if state.status == DRAFT:
        return [("submit", [lambda s: submit_request(s, defn, samples)])]
    if state.status != ACTIVE:
        return []
    out: list[tuple[str, list[Command]]] = []
    for step in state.steps:
        if step.status != READY:
            continue
        sd = defn.step(step.id)
        if sd.type == "manual":
            ticks: list[Command] = [
                (lambda s, sid=step.id, i=i: set_checklist_item(s, defn, sid, i, True))
                for i in range(len(sd.checklist)) if i not in step.ticked
            ]
            out.append((f"complete {step.id}", ticks + [lambda s, sid=step.id: complete_step(s, defn, sid)]))
        elif sd.type == "form":
            out.append((f"submit {step.id}",
                        [lambda s, sid=step.id: complete_form(s, defn, sid, samples.get(sid, {}))]))
    return out


def _form_step(state: RequestState, defn: WorkflowDef, step_id: str) -> StepState:
    """A form step that can be edited now: an initial form in Draft, or a ready form when Active."""
    try:
        sd = defn.step(step_id)
    except KeyError:
        raise CommandError(f"Unknown step {step_id!r}") from None
    if sd.type != "form":
        raise CommandError(f"Step {step_id!r} is not a form")
    if state.status == DRAFT:
        if sd not in initial_forms(defn):
            raise CommandError(f"Step {step_id!r} can't be filled in until the request is submitted")
        return state.step(step_id)
    return _ready_step(state, defn, step_id)


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
        return RequestState(
            request_id=event.request_id, workflow=defn.key, title=event.payload["title"],
            status=DRAFT, created_by=event.actor, created_at=event.ts, updated_at=event.ts,
            seq=event.seq, last_event=event.event_id,
            steps=tuple(StepState(s.id, PENDING) for s in defn.steps),
        )

    if state is None:
        raise ValueError(f"{event.request_id}: {event.type} before {REQUEST_CREATED}")

    if event.type in ANNOTATIONS:
        return _annotate(state, event)

    state = replace(state, seq=event.seq, last_event=event.event_id, updated_at=event.ts)

    if event.type == FORM_SAVED:
        step = state.step(event.payload["step"])
        return _replace_step(state, replace(step, draft=event.payload["data"]))

    if event.type == REQUEST_SUBMITTED:
        inputs: dict = {}
        for step_id, data in event.payload["forms"].items():
            inputs.update(data)
            step = state.step(step_id)
            state = _replace_step(state, replace(step, status=DONE, data=data, draft=None,
                                                 completed_by=event.actor, completed_at=event.ts))
        state = replace(state, status=ACTIVE, inputs=inputs, submitted_by=event.actor, submitted_at=event.ts)
        return _advance(state, defn)

    if event.type == CHECKLIST_ITEM_SET:
        step = state.step(event.payload["step"])
        idx = event.payload["index"]
        ticked = step.ticked | {idx} if event.payload["checked"] else step.ticked - {idx}
        return _replace_step(state, replace(step, ticked=frozenset(ticked)))

    if event.type == STEP_COMPLETED:
        step = state.step(event.payload["step"])
        state = _replace_step(state, replace(step, status=DONE, completed_by=event.actor, completed_at=event.ts,
                                             data=event.payload.get("data", step.data), draft=None))
        return _advance(state, defn)

    raise ValueError(f"Unknown event type {event.type!r}")


def _annotate(state: RequestState, event: Event) -> RequestState:
    if event.type == REFERENCE_ASSIGNED:
        # The earliest assignment wins if a retry ever produced two.
        return state if state.reference else replace(state, reference=event.payload["reference"])
    if event.type == NOTE_ADDED:
        return replace(state, notes=state.notes + (Note(event.ts, event.actor, event.payload["text"]),))
    raise ValueError(f"Unknown annotation {event.type!r}")


def _replace_step(state: RequestState, new: StepState) -> RequestState:
    return replace(state, steps=tuple(new if s.id == new.id else s for s in state.steps))


def _advance(state: RequestState, defn: WorkflowDef) -> RequestState:
    """Make pending steps ready once their dependencies are done; complete the request when all are done."""
    if state.status == DRAFT:
        return state
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
    """Split one request's chain events into the winning chain and the losers.

    Order is by per-request sequence, never wall-clock. When two users act at
    once, both events claim the same sequence; the one with the earliest
    timestamp (then lowest event id) wins. Any event decided on top of a loser
    (its `prev` is a loser) also loses. Events after a gap in the sequence are
    neither: they wait until their predecessor has been read. Annotations
    (seq 0) are not part of the chain and are ignored here.
    """
    by_seq: dict[int, list[Event]] = {}
    for e in events:
        if e.seq != ANNOTATION_SEQ:
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


def annotations(events: Iterable[Event]) -> list[Event]:
    return sorted((e for e in events if e.seq == ANNOTATION_SEQ), key=lambda e: (e.ts, e.event_id))


def replay(events: Iterable[Event], defn: WorkflowDef) -> Replay:
    events = list(events)
    chain, losers = resolve(events)
    state: RequestState | None = None
    for e in chain:
        state = apply(state, e, defn)
    if state is None:
        raise ValueError("No events to replay")
    for e in annotations(events):
        state = apply(state, e, defn)
    return Replay(state, tuple(losers))
