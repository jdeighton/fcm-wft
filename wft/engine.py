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
REQUEST_HELD = "request_held"          # payload: reason
REQUEST_RESUMED = "request_resumed"
REQUEST_CANCELLED = "request_cancelled"  # payload: reason (the request moves to Cancelling)
REQUEST_AMENDED = "request_amended"      # payload: forms (new initial form data), changes [{field, before, after}]
FORM_SAVED = "form_saved"
CHECKLIST_ITEM_SET = "checklist_item_set"
STEP_STARTED = "step_started"
STEP_WAITING = "step_waiting"    # payload: step, reason, note
STEP_RESUMED = "step_resumed"
STEP_COMPLETED = "step_completed"
SUBWORKFLOW_STARTED = "subworkflow_started"  # payload: step, workflow, children [{id, title, index}]
DECISION_MADE = "decision_made"              # payload: step, chosen (when no rule matched)
APPROVAL_REJECTED = "approval_rejected"      # payload: step, notes
VERIFY_CHECKED = "verify_checked"  # payload: step, check {outcome, key, expected, actual, differences, source, error}
REFERENCE_ASSIGNED = "reference_assigned"
NOTE_ADDED = "note_added"
OWNER_CHANGED = "owner_changed"
STEP_ASSIGNMENT = "step_assignment"  # payload: step, mode (claim | assign | release), assignee
ATTACHMENT_ADDED = "attachment_added"  # payload: name, size, sha256, path, step
FLAG_RESOLVED = "flag_resolved"  # payload: flag, resolution, notes, linked_request
ANNOTATIONS = frozenset({REFERENCE_ASSIGNED, NOTE_ADDED, OWNER_CHANGED, STEP_ASSIGNMENT, ATTACHMENT_ADDED,
                         FLAG_RESOLVED})
ANNOTATION_SEQ = 0

# Step types this version of the engine can run. Definitions using others fail validation.
SUPPORTED_STEP_TYPES = frozenset({"manual", "form", "verify", "subworkflow", "decision", "approval"})

# Outcomes of checking a verify step against a trading system
MATCH, MISMATCH, UNAVAILABLE = "match", "mismatch", "unavailable"

DRAFT = "Draft"
ACTIVE = "Active"
ON_HOLD = "OnHold"
CANCELLING = "Cancelling"
CANCELLED = "Cancelled"
REJECTED = "Rejected"  # reached via external-approval outcomes (#12)
COMPLETED = "Completed"
TERMINAL = frozenset({COMPLETED, CANCELLED, REJECTED})

# Impact flags
HOLD, CANCEL, AMEND = "hold", "cancel", "amend"
ACTIONED, NOT_REQUIRED, DEFERRED = "actioned", "not_required", "deferred"
RESOLUTIONS = {ACTIONED: "Actioned", NOT_REQUIRED: "Not required", DEFERRED: "Deferred"}
GENERIC_ROLLBACK = "Manual rollback may be required"

PENDING = "pending"
READY = "ready"
IN_PROGRESS = "in_progress"
WAITING = "waiting"
DONE = "done"
DONE_OUTSIDE = "done_outside_tool"  # done outside WFT (e.g. in the trading system directly), with notes
SKIPPED = "skipped"
OPEN_STEP = frozenset({READY, IN_PROGRESS, WAITING})  # steps someone needs to do something about
WORKABLE = frozenset({READY, IN_PROGRESS})           # steps that can be worked on right now
FINISHED_STEP = frozenset({DONE, DONE_OUTSIDE, SKIPPED})
WORKED_ON = frozenset({IN_PROGRESS, WAITING, DONE, DONE_OUTSIDE})  # someone may have changed something outside WFT

WAIT_REASONS = {
    "client": "Client",
    "internal": "Internal team",
    "krm22": "KRM22",
    "trading_system": "Trading system",
    "other": "Other",
}

MAX_NOTE_LENGTH = 4000
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


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
class Transition:
    """A step entering a state. Timings are derived from these."""
    ts: str
    status: str
    actor: str
    reason: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class StepState:
    id: str
    status: str
    ticked: frozenset[int] = frozenset()
    completed_by: str | None = None
    completed_at: str | None = None
    data: dict | None = None    # submitted form data
    draft: dict | None = None   # form data saved but not yet submitted
    assignee: str | None = None  # who has claimed or been assigned the step
    transitions: tuple[Transition, ...] = ()

    @property
    def since(self) -> str | None:
        """When the step entered its current state."""
        return self.transitions[-1].ts if self.transitions else None

    @property
    def wait_reason(self) -> str | None:
        return self.transitions[-1].reason if self.status == WAITING and self.transitions else None

    @property
    def wait_note(self) -> str | None:
        return self.transitions[-1].note if self.status == WAITING and self.transitions else None


@dataclass(frozen=True)
class Note:
    ts: str
    actor: str
    text: str


@dataclass(frozen=True)
class Attachment:
    ts: str
    actor: str
    name: str        # original file name, for display and download
    size: int
    sha256: str
    path: str        # relative to the share root
    step: str | None = None


@dataclass(frozen=True)
class Hold:
    start: str
    by: str
    reason: str
    end: str | None = None
    by_parent: str | None = None  # set when the hold came from the parent request being held


@dataclass(frozen=True)
class Flag:
    """Work done outside WFT that a lifecycle change may need someone to deal with."""
    id: str
    step: str | None
    trigger: str           # hold | cancel | amend
    action: str            # what needs doing
    compensation: str | None
    raised_by: str
    raised_at: str
    resolution: str | None = None  # actioned | not_required | deferred
    notes: str | None = None
    linked_request: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    child: str | None = None  # the child request this flag is about, if any

    @property
    def blocking(self) -> bool:
        """Open flags stop a request reaching a final state; deferred ones stay visible but don't."""
        return self.resolution is None

    @property
    def open(self) -> bool:
        return self.resolution in (None, DEFERRED)


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
    owner: str = ""  # accountable person; the creator unless reassigned
    completed_at: str | None = None
    attachments: tuple[Attachment, ...] = ()
    holds: tuple[Hold, ...] = ()
    flags: tuple[Flag, ...] = ()
    cancel_reason: str | None = None
    cancelled_at: str | None = None
    follow_up_of: str | None = None
    parent: dict | None = None  # {request, step, index} for a child request started by a sub-workflow step

    def flag(self, flag_id: str) -> Flag:
        for f in self.flags:
            if f.id == flag_id:
                return f
        raise KeyError(flag_id)

    @property
    def blocking_flags(self) -> list[Flag]:
        return [f for f in self.flags if f.blocking]

    @property
    def awaiting_flags(self) -> bool:
        """Every step is finished, but open flags stop the request completing."""
        return self.status == ACTIVE and all(s.status in FINISHED_STEP for s in self.steps) and bool(self.blocking_flags)

    def attachment(self, sha256: str) -> Attachment:
        for a in self.attachments:
            if a.sha256 == sha256:
                return a
        raise KeyError(sha256)

    def step(self, step_id: str) -> StepState:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    @property
    def current_step(self) -> str | None:
        return next((s.id for s in self.steps if s.status in OPEN_STEP), None)


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

def create_request(defn: WorkflowDef, title: str, follow_up_of: str | None = None,
                   parent: dict | None = None) -> NewEvent:
    title = title.strip()
    if not title:
        raise CommandError("A title is required")
    payload: dict[str, Any] = {"title": title}
    if follow_up_of:
        payload["follow_up_of"] = follow_up_of
    if parent:
        payload["parent"] = parent
        payload["auto"] = True
    return NewEvent(REQUEST_CREATED, seq=1, payload=payload)


# --- sub-workflows ---------------------------------------------------------------

def plan_children(state: RequestState, defn: WorkflowDef, step_id: str) -> list[dict]:
    """The child requests a sub-workflow step should start: [{title, inputs, index}], one per `for_each` item."""
    from wft.expressions import parse
    sd = defn.step(step_id)
    ctx = context(state)
    items: list[Any] = [None]
    if sd.raw.get("for_each"):
        value = parse(sd.raw["for_each"]).render(ctx)
        items = list(value) if isinstance(value, (list, tuple)) else []
    out = []
    for index, item in enumerate(items):
        item_ctx = {**ctx, "item": item}
        rendered = {k: parse(v).render(item_ctx) if isinstance(v, str) else v
                    for k, v in (sd.raw.get("map_inputs") or {}).items()}
        inputs = {k: v for k, v in rendered.items() if v not in (None, "")}  # missing values are left out
        label = _item_label(item, index) if sd.raw.get("for_each") else None
        title = f"{state.reference or state.title} – {sd.title}" + (f" – {label}" if label else "")
        out.append({"title": title, "inputs": inputs, "index": index})
    return out


def _item_label(item: Any, index: int) -> str:
    if isinstance(item, dict):
        for key in ("name", "title", "id", "account", "username"):
            if item.get(key):
                return str(item[key])
        return f"#{index + 1}"
    return str(item) if item not in (None, "") else f"#{index + 1}"


def start_subworkflow(state: RequestState, defn: WorkflowDef, step_id: str, workflow: str,
                      children: list[dict]) -> NewEvent:
    step = _workable_step(state, defn, step_id)
    if defn.step(step_id).type != "subworkflow":
        raise CommandError(f"Step {step_id!r} is not a sub-workflow")
    if (step.data or {}).get("children") is not None:
        raise CommandError("Sub-requests have already been started for this step")
    return NewEvent(SUBWORKFLOW_STARTED, seq=state.seq + 1, prev=state.last_event, payload={
        "step": step_id, "workflow": workflow, "auto": True,
        "children": [{"id": c["id"], "title": c["title"], "index": c["index"]} for c in children]})


def complete_subworkflow(state: RequestState, defn: WorkflowDef, step_id: str,
                         child_status: dict[str, tuple[str, str | None]]) -> NewEvent:
    """Join: complete the step once every child request is Completed. `child_status`: id -> (status, reference)."""
    step = _workable_step(state, defn, step_id)
    children = (step.data or {}).get("children")
    if children is None:
        raise CommandError("Sub-requests haven't been started yet")
    not_done = [c for c in children if child_status.get(c["id"], ("?", None))[0] != COMPLETED]
    if not_done:
        raise CommandError(f"{len(not_done)} sub-request(s) not completed yet")
    summary = [{**c, "status": child_status[c["id"]][0], "reference": child_status[c["id"]][1]} for c in children]
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={
        "step": step_id, "auto": True, "data": {**(step.data or {}), "children": summary}})


# --- decisions and approvals -----------------------------------------------------

def decision_targets(sd: StepDef) -> list[str]:
    targets = [b["next"] for b in sd.raw.get("branches", ())]
    if sd.raw.get("default"):
        targets.append(sd.raw["default"])
    return list(dict.fromkeys(targets))


def evaluate_decision(state: RequestState, sd: StepDef) -> tuple[str, str] | None:
    """(chosen step, which rule) for a decision, or None if no rule matches and there's no default."""
    from wft.expressions import ExpressionError, parse
    if sd.raw.get("_choose_by_hand"):  # the validator's dry run explores every option instead
        return None
    ctx = context(state)
    for i, branch in enumerate(sd.raw.get("branches", ())):
        try:
            if parse(branch["when"]).render(ctx):
                return branch["next"], f"rule {i + 1}: {branch['when']}"
        except ExpressionError:
            continue
    if sd.raw.get("default"):
        return sd.raw["default"], "default"
    return None


def choose_branch(state: RequestState, defn: WorkflowDef, step_id: str, chosen: str) -> NewEvent:
    """For a decision no rule settled: a person picks the way on."""
    _workable_step(state, defn, step_id)
    sd = defn.step(step_id)
    if sd.type != "decision":
        raise CommandError(f"Step {step_id!r} is not a decision")
    if chosen not in decision_targets(sd):
        raise CommandError("Choose one of the decision's options")
    return NewEvent(DECISION_MADE, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id, "chosen": chosen})


def approve(state: RequestState, defn: WorkflowDef, step_id: str, me: str, notes: str = "") -> NewEvent:
    step = _approval_step(state, defn, step_id)
    if me == state.submitted_by and not defn.step(step_id).raw.get("allow_submitter"):
        raise CommandError("Someone other than the person who submitted the request must approve it")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={
        "step": step_id, "data": {**(step.data or {}), "approval": "approved", "notes": notes.strip() or None}})


def reject_approval(state: RequestState, defn: WorkflowDef, step_id: str, notes: str) -> NewEvent:
    _approval_step(state, defn, step_id)
    notes = notes.strip()
    if not notes:
        raise CommandError("Say why it isn't approved, so it can be put right")
    return NewEvent(APPROVAL_REJECTED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id, "notes": notes})


def _approval_step(state: RequestState, defn: WorkflowDef, step_id: str) -> StepState:
    step = _workable_step(state, defn, step_id)
    if defn.step(step_id).type != "approval":
        raise CommandError(f"Step {step_id!r} is not an approval")
    return step


# --- amendments --------------------------------------------------------------

_MISSING = object()


def field_changes(before: Any, after: Any, prefix: str = "") -> list[tuple[str, Any, Any]]:
    """Dotted field paths whose values differ. Lists are compared whole (e.g. `users`)."""
    if isinstance(before, dict) and isinstance(after, dict):
        out = []
        for key in sorted(set(before) | set(after), key=str):
            out += field_changes(before.get(key, _MISSING), after.get(key, _MISSING), f"{prefix}{key}.")
        return out
    if before == after:
        return []
    clean = lambda v: None if v is _MISSING else v  # noqa: E731
    return [(prefix.rstrip("."), clean(before), clean(after))]


def consumes_match(pattern: str, path: str) -> bool:
    """Whether a `consumes` pattern covers a changed field. Either may be a prefix of the other; `*` matches any part."""
    p, c = pattern.split("."), path.split(".")
    return all(a == "*" or a == b for a, b in zip(p, c))


def _descendants(defn: WorkflowDef, step_id: str) -> list[str]:
    """Steps that depend, directly or not, on `step_id`, in definition order."""
    found, changed = {step_id}, True
    while changed:
        changed = False
        for s in defn.steps:
            if s.id not in found and found & set(s.depends_on):
                found.add(s.id)
                changed = True
    return [s.id for s in defn.steps if s.id in found and s.id != step_id]


@dataclass(frozen=True)
class Impact:
    changes: list[tuple[str, Any, Any]]
    flagged: list[tuple[str, str, list[str]]]  # (step, action text, fields it uses that changed)
    route_to: list[str]                        # steps the workflow goes back to
    reset: list[str]                           # steps reset to be done again (route targets and what follows)


def amend_impact(state: RequestState, defn: WorkflowDef, new_inputs: dict) -> Impact:
    """What amending the inputs to `new_inputs` would change. Pure; used for the preview and when applying."""
    changes = field_changes(state.inputs, new_inputs)
    changed = [c[0] for c in changes]
    initial = {sd.id for sd in initial_forms(defn)}
    flagged, routes = [], []
    for st in state.steps:
        sd = defn.step(st.id)
        if st.id in initial or st.status not in WORKED_ON:
            continue  # steps not started yet simply use the new data
        hits = sorted({f for f in changed for pattern in sd.raw.get("consumes", ()) if consumes_match(pattern, f)})
        if not hits:
            continue
        hook = sd.raw.get("on_amend") or {}
        action = hook.get("flag") or f"Needs re-check: {', '.join(hits)} changed"
        flagged.append((st.id, action, hits))
        if hook.get("route_to") and hook["route_to"] not in routes:
            routes.append(hook["route_to"])
    reset: list[str] = []
    for target in routes:
        for sid in [target, *_descendants(defn, target)]:
            if sid not in reset and state.step(sid).status != PENDING:
                reset.append(sid)
    return Impact(changes, flagged, routes, reset)


def amend_request(state: RequestState, defn: WorkflowDef, forms: dict[str, dict]) -> NewEvent:
    """Change the request's inputs (its initial form data). Validated like a submit."""
    if state.status not in (ACTIVE, ON_HOLD):
        raise CommandError(f"Only Active or on-hold requests can be amended (this one is {state.status})")
    capture = initial_forms(defn)
    if not capture:
        raise CommandError("This workflow has no input form to amend")
    data, new_inputs = {}, {}
    for sd in capture:
        values = forms.get(sd.id, {})
        errors = validate_form(sd.form_schema or {}, values)
        if errors:
            raise FormInvalid(sd.id, errors)
        data[sd.id] = values
        new_inputs.update(values)
    changes = field_changes(state.inputs, new_inputs)
    if not changes:
        raise CommandError("Nothing has changed")
    return NewEvent(REQUEST_AMENDED, seq=state.seq + 1, prev=state.last_event, payload={
        "forms": data, "changes": [{"field": f, "before": b, "after": a} for f, b, a in changes]})


# --- verify steps ---------------------------------------------------------------

def context(state: RequestState) -> dict:
    """What definition expressions can see: inputs, step data and the request."""
    return {"inputs": state.inputs, "request": {"reference": state.reference, "title": state.title},
            "steps": {s.id: {**(s.data or {}), "status": s.status} for s in state.steps}}


def verify_target(state: RequestState, defn: WorkflowDef, step_id: str) -> tuple[str, Any, Any]:
    """(adapter operation, key, expected values) for a verify step, with expressions filled in."""
    from wft.expressions import parse
    sd = defn.step(step_id)
    if sd.type != "verify":
        raise CommandError(f"Step {step_id!r} is not a verify step")
    ctx = context(state)
    render = lambda v: parse(v).render(ctx) if isinstance(v, str) else v  # noqa: E731
    return sd.raw["adapter"], render(sd.raw.get("key")), render(sd.raw.get("expected"))


def record_check(state: RequestState, defn: WorkflowDef, step_id: str, check: dict) -> NewEvent:
    """Record the result of checking a verify step against the trading system (done by the caller)."""
    _verify_step(state, defn, step_id)
    if check.get("outcome") not in (MATCH, MISMATCH, UNAVAILABLE):
        raise CommandError("Unknown check outcome")
    return NewEvent(VERIFY_CHECKED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id, "check": check})


def accept_verification(state: RequestState, defn: WorkflowDef, step_id: str, notes: str = "") -> NewEvent:
    step = _verify_step(state, defn, step_id)
    last = (step.data or {}).get("last_check")
    if not last or last["outcome"] == UNAVAILABLE:
        raise CommandError("Check it against the trading system first, or complete it manually with notes")
    notes = notes.strip()
    if last["outcome"] == MISMATCH and not notes:
        raise CommandError("Say why the differences are acceptable")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={
        "step": step_id, "data": {"result": "accepted", "notes": notes or None, "last_check": last}})


def complete_verification_manually(state: RequestState, defn: WorkflowDef, step_id: str, notes: str) -> NewEvent:
    """When the trading system can't be checked from WFT: someone checked by hand."""
    step = _verify_step(state, defn, step_id)
    notes = notes.strip()
    if not notes:
        raise CommandError("Say how it was checked")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={
        "step": step_id, "data": {"result": "manual", "notes": notes, "last_check": (step.data or {}).get("last_check")}})


def _verify_step(state: RequestState, defn: WorkflowDef, step_id: str) -> StepState:
    step = _workable_step(state, defn, step_id)
    if defn.step(step_id).type != "verify":
        raise CommandError(f"Step {step_id!r} is not a verify step")
    return step


def hold_request(state: RequestState, defn: WorkflowDef, reason: str, child_flags: list[dict] = (),
                 by_parent: str | None = None) -> NewEvent:
    """`child_flags` ([{child, step, action}]) are worked out by the caller, which can see the child requests."""
    if state.status != ACTIVE:
        raise CommandError(f"Only Active requests can be put on hold (this one is {state.status})")
    reason = reason.strip()
    if not reason:
        raise CommandError("Give a reason for the hold")
    payload: dict[str, Any] = {"reason": reason}
    if child_flags:
        payload["child_flags"] = list(child_flags)
    if by_parent:
        payload.update(by_parent=by_parent, auto=True)
    return NewEvent(REQUEST_HELD, seq=state.seq + 1, prev=state.last_event, payload=payload)


def resume_request(state: RequestState, defn: WorkflowDef, auto: bool = False) -> NewEvent:
    if state.status != ON_HOLD:
        raise CommandError(f"Request isn't on hold (it's {state.status})")
    return NewEvent(REQUEST_RESUMED, seq=state.seq + 1, prev=state.last_event, payload={"auto": True} if auto else {})


def cancel_request(state: RequestState, defn: WorkflowDef, reason: str, child_flags: list[dict] = (),
                   auto: bool = False) -> NewEvent:
    if state.status not in (DRAFT, ACTIVE, ON_HOLD):
        raise CommandError(f"A {state.status} request can't be cancelled")
    reason = reason.strip()
    if not reason:
        raise CommandError("Give a reason for cancelling")
    payload: dict[str, Any] = {"reason": reason}
    if child_flags:
        payload["child_flags"] = list(child_flags)
    if auto:
        payload["auto"] = True
    return NewEvent(REQUEST_CANCELLED, seq=state.seq + 1, prev=state.last_event, payload=payload)


def resolve_flag(state: RequestState, flag_id: str, resolution: str, notes: str = "",
                 linked_request: str | None = None) -> NewEvent:
    try:
        flag = state.flag(flag_id)
    except KeyError:
        raise CommandError("Unknown flag") from None
    if not flag.open:
        raise CommandError(f"This flag was already resolved as {RESOLUTIONS[flag.resolution].lower()}")
    if resolution not in RESOLUTIONS:
        raise CommandError("Choose actioned, not required or deferred")
    notes = notes.strip()
    if not notes:
        raise CommandError({ACTIONED: "Say what was done", NOT_REQUIRED: "Say why it isn't required",
                            DEFERRED: "Say why it's deferred and what's left to do"}[resolution])
    if flag.resolution == DEFERRED and resolution == DEFERRED:
        raise CommandError("This flag is already deferred")
    return NewEvent(FLAG_RESOLVED, seq=ANNOTATION_SEQ, payload={
        "flag": flag_id, "resolution": resolution, "notes": notes, "linked_request": linked_request or None})


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
    step = _workable_step(state, defn, step_id)
    sd = defn.step(step_id)
    if sd.type != "form":
        raise CommandError(f"Step {step_id!r} is not a form")
    errors = validate_form(sd.form_schema or {}, data)
    if errors:
        raise FormInvalid(step_id, errors)
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event,
                    payload={"step": step.id, "data": data})


def set_checklist_item(state: RequestState, defn: WorkflowDef, step_id: str, index: int, checked: bool) -> NewEvent:
    step = _workable_step(state, defn, step_id)
    if not 0 <= index < len(defn.step(step_id).checklist):
        raise CommandError(f"Step {step_id!r} has no checklist item {index}")
    if (index in step.ticked) == checked:
        raise CommandError("Checklist item is already in that state")
    return NewEvent(CHECKLIST_ITEM_SET, seq=state.seq + 1, prev=state.last_event,
                    payload={"step": step_id, "index": index, "checked": checked})


def complete_step(state: RequestState, defn: WorkflowDef, step_id: str) -> NewEvent:
    step = _workable_step(state, defn, step_id)
    if defn.step(step_id).type == "form":
        raise CommandError(f"Step {step_id!r} is a form; submit the form to complete it")
    if defn.step(step_id).type == "verify":
        raise CommandError(f"Step {step_id!r} is a check; check it, then accept it or complete it manually")
    if defn.step(step_id).type in ("subworkflow", "decision", "approval"):
        raise CommandError(f"Step {step_id!r} completes itself ({defn.step(step_id).type})")
    missing = len(defn.step(step_id).checklist) - len(step.ticked)
    if missing:
        raise CommandError(f"{missing} checklist item(s) still to tick")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id})


def complete_outside_tool(state: RequestState, defn: WorkflowDef, step_id: str, notes: str) -> NewEvent:
    """The step was done outside WFT (for example straight in the trading system). Notes are mandatory."""
    _workable_step(state, defn, step_id)
    notes = notes.strip()
    if not notes:
        raise CommandError("Say what was done outside WFT, and why")
    return NewEvent(STEP_COMPLETED, seq=state.seq + 1, prev=state.last_event,
                    payload={"step": step_id, "outside_tool": True, "notes": notes})


def start_step(state: RequestState, defn: WorkflowDef, step_id: str) -> NewEvent:
    step = _workable_step(state, defn, step_id)
    if step.status != READY:
        raise CommandError(f"Step {step_id!r} has already started")
    return NewEvent(STEP_STARTED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id})


def wait_step(state: RequestState, defn: WorkflowDef, step_id: str, reason: str, note: str = "") -> NewEvent:
    """Mark a step as waiting on someone or something, so the wait is timed and attributed."""
    _workable_step(state, defn, step_id)
    if reason not in WAIT_REASONS:
        raise CommandError(f"Choose what the step is waiting on ({', '.join(WAIT_REASONS.values())})")
    note = note.strip()
    if reason == "other" and not note:
        raise CommandError("Say what the step is waiting on")
    return NewEvent(STEP_WAITING, seq=state.seq + 1, prev=state.last_event,
                    payload={"step": step_id, "reason": reason, "note": note or None})


def resume_step(state: RequestState, defn: WorkflowDef, step_id: str) -> NewEvent:
    step = _open_step(state, defn, step_id)
    if step.status != WAITING:
        raise CommandError(f"Step {step_id!r} isn't waiting")
    return NewEvent(STEP_RESUMED, seq=state.seq + 1, prev=state.last_event, payload={"step": step_id})


def assign_reference(state: RequestState, reference: str) -> NewEvent:
    if state.status == DRAFT:
        raise CommandError("References are assigned when a request is submitted")
    if state.reference:
        raise CommandError(f"Request already has reference {state.reference}")
    return NewEvent(REFERENCE_ASSIGNED, seq=ANNOTATION_SEQ, payload={"reference": reference})


def change_owner(state: RequestState, new_owner: str) -> NewEvent:
    if state.status in TERMINAL:
        raise CommandError(f"A {state.status} request can't be changed; create a follow-up request instead")
    new_owner = new_owner.strip().lower()
    if not new_owner:
        raise CommandError("Choose who should own the request")
    if new_owner == state.owner:
        raise CommandError(f"{new_owner} already owns this request")
    return NewEvent(OWNER_CHANGED, seq=ANNOTATION_SEQ, payload={"owner": new_owner, "previous": state.owner})


def claim_step(state: RequestState, defn: WorkflowDef, step_id: str, me: str) -> NewEvent:
    step = _open_step(state, defn, step_id)
    if step.assignee == me:
        raise CommandError("You already have this step")
    if step.assignee:
        raise CommandError(f"{step.assignee} has this step; ask them to release it, or assign it to yourself")
    return NewEvent(STEP_ASSIGNMENT, seq=ANNOTATION_SEQ, payload={"step": step_id, "mode": "claim", "assignee": me})


def assign_step(state: RequestState, defn: WorkflowDef, step_id: str, user: str) -> NewEvent:
    step = _open_step(state, defn, step_id)
    user = user.strip().lower()
    if not user:
        raise CommandError("Choose who to assign the step to")
    if step.assignee == user:
        raise CommandError(f"{user} already has this step")
    return NewEvent(STEP_ASSIGNMENT, seq=ANNOTATION_SEQ,
                    payload={"step": step_id, "mode": "assign", "assignee": user, "previous": step.assignee})


def release_step(state: RequestState, defn: WorkflowDef, step_id: str) -> NewEvent:
    step = _open_step(state, defn, step_id)
    if not step.assignee:
        raise CommandError("Nobody has this step")
    return NewEvent(STEP_ASSIGNMENT, seq=ANNOTATION_SEQ,
                    payload={"step": step_id, "mode": "release", "assignee": None, "previous": step.assignee})


def check_step_exists(defn: WorkflowDef, step_id: str | None) -> None:
    if step_id:
        try:
            defn.step(step_id)
        except KeyError:
            raise CommandError(f"Unknown step {step_id!r}") from None


def add_attachment(state: RequestState, defn: WorkflowDef, name: str, size: int, sha256: str, path: str,
                   step_id: str | None = None) -> NewEvent:
    """Record a file already stored on the share. Allowed at any stage, so evidence can be added afterwards."""
    if not name.strip():
        raise CommandError("The file needs a name")
    if size > MAX_ATTACHMENT_BYTES:
        raise CommandError(f"Files are limited to {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB")
    check_step_exists(defn, step_id)
    if any(a.sha256 == sha256 and a.name == name for a in state.attachments):
        raise CommandError(f"{name} is already attached")
    return NewEvent(ATTACHMENT_ADDED, seq=ANNOTATION_SEQ, payload={
        "name": name, "size": size, "sha256": sha256, "path": path, "step": step_id or None})


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
        if step.status not in WORKABLE:
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
        elif sd.type == "verify":
            out.append((f"verify {step.id}",
                        [lambda s, sid=step.id: complete_verification_manually(s, defn, sid, "dry run")]))
        elif sd.type == "subworkflow":
            out.append((f"run sub-requests {step.id}", [
                lambda s, sid=step.id: start_subworkflow(s, defn, sid, sd.raw["workflow"], []),
                lambda s, sid=step.id: complete_subworkflow(s, defn, sid, {})]))
        elif sd.type == "decision":
            for target in decision_targets(sd):
                out.append((f"choose {target} at {step.id}",
                            [lambda s, sid=step.id, t=target: choose_branch(s, defn, sid, t)]))
        elif sd.type == "approval":
            out.append((f"approve {step.id}", [lambda s, sid=step.id: approve(s, defn, sid, "dry-run-approver")]))
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
    return _workable_step(state, defn, step_id)


def _open_step(state: RequestState, defn: WorkflowDef, step_id: str) -> StepState:
    """A step that's ready, in progress or waiting on an Active request."""
    if state.status != ACTIVE:
        raise CommandError(f"Request is {state.status}")
    try:
        step = state.step(step_id)
    except KeyError:
        raise CommandError(f"Unknown step {step_id!r}") from None
    if step.status not in OPEN_STEP:
        raise CommandError(f"Step {step_id!r} is {step.status.replace('_', ' ')}")
    return step


def _workable_step(state: RequestState, defn: WorkflowDef, step_id: str) -> StepState:
    """A step that can be worked on now: ready or in progress (a waiting step must be resumed first)."""
    step = _open_step(state, defn, step_id)
    if step.status == WAITING:
        raise CommandError(f"Step {step_id!r} is waiting on {WAIT_REASONS[step.wait_reason].lower()}; "
                           "resume it to carry on")
    return step


# --- folding events into state -------------------------------------------

def apply(state: RequestState | None, event: Event, defn: WorkflowDef) -> RequestState:
    if event.type == REQUEST_CREATED:
        if state is not None:
            raise ValueError(f"{event.request_id}: duplicate {REQUEST_CREATED}")
        return RequestState(
            request_id=event.request_id, workflow=defn.key, title=event.payload["title"],
            status=DRAFT, created_by=event.actor, created_at=event.ts, updated_at=event.ts,
            seq=event.seq, last_event=event.event_id, owner=event.actor,
            steps=tuple(StepState(s.id, PENDING) for s in defn.steps),
            follow_up_of=event.payload.get("follow_up_of"), parent=event.payload.get("parent"),
        )

    if state is None:
        raise ValueError(f"{event.request_id}: {event.type} before {REQUEST_CREATED}")

    if event.type in ANNOTATIONS:
        return _annotate(state, event)[0]

    state = replace(state, seq=event.seq, last_event=event.event_id, updated_at=event.ts)

    if event.type == REQUEST_HELD:
        flags = _raise_flags(state, defn, event, HOLD) + _child_flags(event, HOLD)
        hold = Hold(event.ts, event.actor, event.payload["reason"], by_parent=event.payload.get("by_parent"))
        return replace(state, status=ON_HOLD, flags=state.flags + flags, holds=state.holds + (hold,))

    if event.type == REQUEST_RESUMED:
        return replace(state, status=ACTIVE, holds=_end_hold(state.holds, event.ts))

    if event.type == REQUEST_CANCELLED:
        flags = (_raise_flags(state, defn, event, CANCEL) if state.status != DRAFT else ()) + _child_flags(event, CANCEL)
        state = replace(state, status=CANCELLING, flags=state.flags + flags, holds=_end_hold(state.holds, event.ts),
                        cancel_reason=event.payload["reason"])
        return _settle(state, event.ts)

    if event.type == REQUEST_AMENDED:
        return _amend(state, defn, event)

    if event.type == FORM_SAVED:
        step = state.step(event.payload["step"])
        if state.status == ACTIVE:
            step = _start(step, event)
        return _replace_step(state, replace(step, draft=event.payload["data"]))

    if event.type == REQUEST_SUBMITTED:
        inputs: dict = {}
        for step_id, data in event.payload["forms"].items():
            inputs.update(data)
            # The capture form was being filled in for the whole Draft period.
            step = replace(state.step(step_id),
                           transitions=(Transition(state.created_at, IN_PROGRESS, state.created_by),))
            step = _to(step, DONE, event)
            state = _replace_step(state, replace(step, data=data, draft=None,
                                                 completed_by=event.actor, completed_at=event.ts))
        state = replace(state, status=ACTIVE, inputs=inputs, submitted_by=event.actor, submitted_at=event.ts)
        return _advance(state, defn, event)

    if event.type == CHECKLIST_ITEM_SET:
        step = _start(state.step(event.payload["step"]), event)
        idx = event.payload["index"]
        ticked = step.ticked | {idx} if event.payload["checked"] else step.ticked - {idx}
        return _replace_step(state, replace(step, ticked=frozenset(ticked)))

    if event.type == SUBWORKFLOW_STARTED:
        step = _start(state.step(event.payload["step"]), event)
        data = {"workflow": event.payload["workflow"], "children": event.payload["children"]}
        return _replace_step(state, replace(step, data={**(step.data or {}), **data}))

    if event.type == DECISION_MADE:
        sd = defn.step(event.payload["step"])
        state = _resolve_decision(state, sd, event, event.payload["chosen"], "chosen by hand")
        return _advance(state, defn, event)

    if event.type == APPROVAL_REJECTED:
        step = _start(state.step(event.payload["step"]), event)
        rejections = [*(step.data or {}).get("rejections", ()),
                      {"ts": event.ts, "by": event.actor, "notes": event.payload["notes"]}]
        return _replace_step(state, replace(step, data={**(step.data or {}), "rejections": rejections}))

    if event.type == VERIFY_CHECKED:
        step = _start(state.step(event.payload["step"]), event)
        check = {**event.payload["check"], "checked_at": event.ts, "checked_by": event.actor}
        return _replace_step(state, replace(step, data={**(step.data or {}), "last_check": check}))

    if event.type == STEP_STARTED:
        return _replace_step(state, _start(state.step(event.payload["step"]), event))

    if event.type == STEP_WAITING:
        step = _start(state.step(event.payload["step"]), event)
        return _replace_step(state, _to(step, WAITING, event, event.payload["reason"], event.payload.get("note")))

    if event.type == STEP_RESUMED:
        return _replace_step(state, _to(state.step(event.payload["step"]), IN_PROGRESS, event))

    if event.type == STEP_COMPLETED:
        outside = event.payload.get("outside_tool", False)
        step = _start(state.step(event.payload["step"]), event)
        step = _to(step, DONE_OUTSIDE if outside else DONE, event, note=event.payload.get("notes"))
        data = {"outside_tool_notes": event.payload["notes"]} if outside else event.payload.get("data", step.data)
        state = _replace_step(state, replace(step, completed_by=event.actor, completed_at=event.ts,
                                             data=data, draft=None))
        return _advance(state, defn, event)

    raise ValueError(f"Unknown event type {event.type!r}")


def _annotate(state: RequestState, event: Event) -> tuple[RequestState, bool]:
    """Apply an annotation (in time order). Returns the new state and whether it took effect."""
    if event.type == REFERENCE_ASSIGNED:
        # The earliest assignment wins if a retry ever produced two.
        if state.reference:
            return state, False
        return replace(state, reference=event.payload["reference"]), True
    if event.type == NOTE_ADDED:
        return replace(state, notes=state.notes + (Note(event.ts, event.actor, event.payload["text"]),)), True
    if event.type == ATTACHMENT_ADDED:
        p = event.payload
        return replace(state, attachments=state.attachments + (
            Attachment(event.ts, event.actor, p["name"], p["size"], p["sha256"], p["path"], p.get("step")),)), True
    if event.type == OWNER_CHANGED:
        return replace(state, owner=event.payload["owner"]), True  # the latest change wins
    if event.type == FLAG_RESOLVED:
        p = event.payload
        try:
            flag = state.flag(p["flag"])
        except KeyError:
            return state, False
        if not flag.open or (flag.resolution == DEFERRED and p["resolution"] == DEFERRED):
            return state, False  # someone else resolved it first
        flag = replace(flag, resolution=p["resolution"], notes=p["notes"], linked_request=p.get("linked_request"),
                       resolved_by=event.actor, resolved_at=event.ts)
        state = replace(state, flags=tuple(flag if f.id == flag.id else f for f in state.flags))
        return _settle(state, max(event.ts, state.updated_at)), True
    if event.type == STEP_ASSIGNMENT:
        try:
            step = state.step(event.payload["step"])
        except KeyError:
            return state, False
        if event.payload["mode"] == "claim" and step.assignee not in (None, event.actor):
            return state, False  # someone else claimed it first: claims are exclusive
        return _replace_step(state, replace(step, assignee=event.payload["assignee"])), True
    raise ValueError(f"Unknown annotation {event.type!r}")


def _amend(state: RequestState, defn: WorkflowDef, event: Event) -> RequestState:
    new_inputs: dict = {}
    for data in event.payload["forms"].values():
        new_inputs.update(data)
    impact = amend_impact(state, defn, new_inputs)
    flags = tuple(
        Flag(id=f"{event.event_id}:{sid}", step=sid, trigger=AMEND, action=action, compensation=None,
             raised_by=event.actor, raised_at=event.ts)
        for sid, action, _ in impact.flagged)
    for step_id, data in event.payload["forms"].items():
        state = _replace_step(state, replace(state.step(step_id), data=data))
    for sid in impact.reset:
        st = state.step(sid)
        target = sid in impact.route_to
        st = _to(st, READY if target else PENDING, event, reason="amended")
        # Done again from scratch; earlier form data is kept as a draft to edit.
        state = _replace_step(state, replace(st, ticked=frozenset(), completed_by=None, completed_at=None,
                                             draft=st.data if st.data is not None else st.draft, data=None))
    state = replace(state, inputs=new_inputs, flags=state.flags + flags)
    return _advance(state, defn, event) if state.status == ACTIVE else state


def _end_hold(holds: tuple[Hold, ...], ts: str) -> tuple[Hold, ...]:
    return tuple(replace(h, end=ts) if h.end is None else h for h in holds)


def _raise_flags(state: RequestState, defn: WorkflowDef, event: Event, trigger: str) -> tuple[Flag, ...]:
    """Flags for steps whose outside-WFT work a hold or cancel may affect.

    Cancel: every started, waiting or done step, except pure data-capture forms
    without an `on_cancel`. Hold: in-flight steps that declare `on_hold`.
    """
    from wft.expressions import ExpressionError, parse  # local: keeps the engine's import surface small

    ctx = context(state)

    def render(text: str | None) -> str | None:
        if not text:
            return None
        try:
            return str(parse(text).render(ctx))
        except ExpressionError:
            return text

    flags = []
    for st in state.steps:
        sd = defn.step(st.id)
        hook = sd.raw.get(f"on_{trigger}") or {}
        if trigger == HOLD:
            if not hook or st.status not in (IN_PROGRESS, WAITING):
                continue
        elif trigger == CANCEL:
            if st.status not in WORKED_ON or (sd.type == "form" and not hook):
                continue
        compensation = render((sd.raw.get("compensation") or {}).get("checklist"))
        flags.append(Flag(id=f"{event.event_id}:{st.id}", step=st.id, trigger=trigger,
                          action=render(hook.get("flag")) or GENERIC_ROLLBACK, compensation=compensation,
                          raised_by=event.actor, raised_at=event.ts))
    return tuple(flags)


def _child_flags(event: Event, trigger: str) -> tuple[Flag, ...]:
    """Flags about child requests, worked out by the caller (which can see them) and carried in the event."""
    return tuple(Flag(id=f"{event.event_id}:{c['step']}:{c['child']}", step=c["step"], trigger=trigger,
                      action=c["action"], compensation=None, raised_by=event.actor, raised_at=event.ts,
                      child=c["child"])
                 for c in event.payload.get("child_flags", ()))


def _resolve_decision(state: RequestState, sd: StepDef, event: Event, chosen: str, rule: str) -> RequestState:
    """Finish a decision: the chosen way on continues; the other options are skipped."""
    step = state.step(sd.id)
    if step.status == READY:
        step = _to(step, IN_PROGRESS, event)
    step = _to(step, DONE, event)
    state = _replace_step(state, replace(step, data={"chosen": chosen, "rule": rule},
                                         completed_by=event.actor, completed_at=event.ts))
    for target in decision_targets(sd):
        other = state.step(target)
        if target != chosen and other.status == PENDING:
            state = _replace_step(state, _to(other, SKIPPED, event, reason=f"not chosen by {sd.id}"))
    return state


def _settle(state: RequestState, ts: str) -> RequestState:
    """Reach a final state once nothing (no open flag) stands in the way."""
    if state.blocking_flags:
        return state
    if state.status == CANCELLING:
        return replace(state, status=CANCELLED, cancelled_at=ts)
    if state.status == ACTIVE and all(s.status in FINISHED_STEP for s in state.steps):
        return replace(state, status=COMPLETED, completed_at=ts)
    return state


def _to(step: StepState, status: str, event: Event, reason: str | None = None, note: str | None = None) -> StepState:
    return replace(step, status=status,
                   transitions=step.transitions + (Transition(event.ts, status, event.actor, reason, note),))


def _start(step: StepState, event: Event) -> StepState:
    """The first work on a ready step starts it."""
    return _to(step, IN_PROGRESS, event) if step.status == READY else step


def _replace_step(state: RequestState, new: StepState) -> RequestState:
    return replace(state, steps=tuple(new if s.id == new.id else s for s in state.steps))


def _advance(state: RequestState, defn: WorkflowDef, event: Event) -> RequestState:
    """Make pending steps ready once their dependencies are finished; complete the request when all are."""
    if state.status == DRAFT:
        return state
    changed = True
    while changed:
        changed = False
        # Decisions settle themselves as soon as they're ready, when a rule (or the default) applies.
        for st in state.steps:
            sd = defn.step(st.id)
            if st.status == READY and sd.type == "decision" and state.status == ACTIVE:
                outcome = evaluate_decision(state, sd)
                if outcome:
                    state = _resolve_decision(state, sd, event, *outcome)
                    changed = True
        by_id = {s.id: s for s in state.steps}
        for st in state.steps:
            deps = defn.step(st.id).depends_on
            if st.status != PENDING or not all(by_id[d].status in FINISHED_STEP for d in deps):
                continue
            # A step whose every dependency was skipped (a branch not taken) is skipped too.
            skipped = bool(deps) and all(by_id[d].status == SKIPPED for d in deps)
            state = _replace_step(state, _to(st, SKIPPED if skipped else READY, event,
                                             reason="branch not taken" if skipped else None))
            changed = True
    return _settle(state, event.ts)


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
        state, effective = _annotate(state, e)
        if not effective and e.type in (STEP_ASSIGNMENT, FLAG_RESOLVED):
            losers.append(e)  # lost to an earlier claim/resolution: reported like any other conflict
    return Replay(state, tuple(losers))
