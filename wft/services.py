"""Application services: run engine commands, stamp and persist events, update the read model."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from wft import __version__, engine, timings
from wft.adapters import Adapter, AdapterUnavailable, NotFoundInSystem, compare, split_operation
from wft.adapters.cache import Picklist, PicklistCache
from wft import offprocess
from wft.configfiles import DEFAULT_REASONS, Reason, ReloadingFile
from wft.definitions import DefinitionRegistry, WorkflowDef
from wft.engine import CommandError, NewEvent, RequestState
from wft.events import Event, new_id, utc_now
from wft.readmodel import ReadModel
from wft.repository import (AttachmentStore, AttachmentTooLarge, ReferenceAllocator, ReferenceUnavailable, Repository,
                            ShareUnavailable)
from wft.team import Team, TeamSource

log = logging.getLogger(__name__)

POLL_SECONDS = 10.0
DEFAULT_REFERENCE_PREFIX = "REQ"


class NotFound(Exception):
    pass


class FileTooLarge(CommandError):
    pass


class ReadOnly(Exception):
    """The share is unavailable, so changes are blocked."""


@dataclass(frozen=True)
class Task:
    request: RequestState
    step_id: str
    step_title: str
    workflow_title: str


@dataclass(frozen=True)
class Tasks:
    mine: list[Task]             # ready steps I've claimed or been assigned
    on_my_requests: list[Task]   # ready, unclaimed steps on requests I own
    claimable: list[Task]        # ready, unclaimed steps on other people's requests
    drafts: list[RequestState]   # my requests not yet submitted
    flagged: list[RequestState] = field(default_factory=list)  # requests I own with open impact flags


@dataclass(frozen=True)
class Row:
    """One line of the Open requests list."""
    request: RequestState
    workflow_title: str
    current_step_title: str | None
    current_step_status: str | None
    age: float
    in_step: float | None


@dataclass(frozen=True)
class Waiting:
    """A step on the Waiting-on board."""
    request: RequestState
    step_id: str
    step_title: str
    reason: str
    note: str | None
    since: str
    by: str
    waited: float


STATUS_FILTERS = {
    "open": (engine.DRAFT, engine.ACTIVE, engine.ON_HOLD, engine.CANCELLING),
    "draft": (engine.DRAFT,),
    "active": (engine.ACTIVE,),
    "on_hold": (engine.ON_HOLD,),
    "cancelling": (engine.CANCELLING,),
    "completed": (engine.COMPLETED,),
    "cancelled": (engine.CANCELLED,),
    "all": None,
}


@dataclass(frozen=True)
class OpenFlag:
    request: RequestState
    flag: engine.Flag
    step_title: str | None
SORT_KEYS = {
    "age": lambda r: r.age,
    "in_step": lambda r: r.in_step if r.in_step is not None else -1,
    "reference": lambda r: r.request.reference or "",
    "title": lambda r: r.request.title.lower(),
    "workflow": lambda r: r.workflow_title.lower(),
    "owner": lambda r: r.request.owner,
    "status": lambda r: r.request.status,
    "created": lambda r: r.request.created_at,
}


@dataclass(frozen=True)
class Status:
    read_only: bool
    reason: str | None
    since: str | None
    version: int


class Services:
    def __init__(self, repo: Repository, read_model: ReadModel, registry: DefinitionRegistry,
                 user: str, host: str, acknowledged_path: Path | None = None,
                 references: ReferenceAllocator | None = None, team: TeamSource | None = None,
                 attachments: AttachmentStore | None = None, adapters: dict[str, Adapter] | None = None,
                 picklists: PicklistCache | None = None,
                 reasons: ReloadingFile[tuple[Reason, ...]] | None = None):
        self.repo = repo
        self.rm = read_model
        self.registry = registry
        self.user = user
        self.host = host
        self._acknowledged_path = acknowledged_path
        self.references = references
        self.team_source = team
        self.attachments = attachments
        self.adapters = adapters or {}
        self.picklists = picklists
        self.reasons_source = reasons
        self._lock = threading.Lock()
        self._read_only_reason: str | None = None
        self._read_only_since: str | None = None
        self._stop = threading.Event()
        self._poller: threading.Thread | None = None

    # --- syncing with the share ------------------------------------------

    def sync(self) -> bool:
        """Read anything new from the share into the read model. Returns False if the share is unavailable."""
        try:
            added = self.registry.refresh()
            if added:
                log.info("Loaded workflow definition(s): %s", ", ".join(added))
            if self.reasons_source is not None:
                self.reasons_source.refresh()
            if self.team_source is not None and self.team_source.refresh():
                log.info("Loaded team configuration: admins %s", ", ".join(self.team.admins) or "none")
            events, cursor = self.repo.read_since(self.rm.cursor())
        except OSError as e:
            self._go_read_only(e)
            return False
        self.rm.ingest(events, cursor)
        if self._read_only_reason is not None:
            log.info("Share reachable again; leaving read-only mode")
            self._read_only_reason = self._read_only_since = None
        self.assign_pending_references()
        return True

    def assign_pending_references(self) -> None:
        """Give a readable reference to any request I submitted that doesn't have one yet."""
        if self.references is None or self._read_only_reason is not None:
            return
        for state in self.rm.list_requests():
            if state.submitted_by == self.user and state.reference is None and state.status != engine.DRAFT:
                self._assign_reference(state.request_id)
        for rec in self.rm.list_off_process():
            if rec.recorded_by == self.user and rec.reference is None:
                self._assign_off_process_reference(rec.record_id)

    def _assign_reference(self, request_id: str) -> None:
        if self.references is None:
            return
        with self._lock:
            state, defn = self._load(request_id)
            if state.reference or state.status == engine.DRAFT:
                return
            prefix = defn.raw.get("reference_prefix") or DEFAULT_REFERENCE_PREFIX
            try:
                reference = self.references.allocate(prefix, int(state.submitted_at[:4]))
            except ReferenceUnavailable as e:
                log.warning("No reference for %s yet: %s", request_id, e)
                return
            try:
                self._record(request_id, defn, engine.assign_reference(state, reference))
            except ReadOnly:
                log.warning("Allocated %s but couldn't record it; a new number will be allocated", reference)

    def start_polling(self, every: float = POLL_SECONDS) -> None:
        def loop():
            while not self._stop.wait(every):
                try:
                    self.sync()
                except Exception:  # keep polling whatever happens
                    log.exception("Sync failed")
        self._poller = threading.Thread(target=loop, name="wft-poller", daemon=True)
        self._poller.start()

    def stop_polling(self) -> None:
        self._stop.set()
        if self._poller:
            self._poller.join(timeout=5)

    def rebuild_cache(self) -> None:
        """Throw away the local cache and re-read every journal."""
        if self.status().read_only:
            raise ReadOnly("The share is unavailable, so the cache can't be rebuilt right now")
        with self._lock:
            self.rm.reset()
            self.sync()

    def status(self) -> Status:
        return Status(self._read_only_reason is not None, self._read_only_reason,
                      self._read_only_since, self.rm.version())

    def _go_read_only(self, error: OSError) -> None:
        if self._read_only_reason is None:
            log.warning("Share unavailable; switching to read-only mode: %s", error)
            self._read_only_since = utc_now()
        self._read_only_reason = str(error) if isinstance(error, ShareUnavailable) else f"Share error: {error}"

    # --- conflicts ---------------------------------------------------------

    def my_conflicts(self) -> list[Event]:
        """My actions that lost to someone else's concurrent action and that I haven't dismissed."""
        acknowledged = self._acknowledged()
        return [e for e in self.rm.conflicts_for(self.user) if e.event_id not in acknowledged]

    def acknowledge_conflict(self, event_id: str) -> None:
        if self._acknowledged_path is None:
            return
        ids = self._acknowledged() | {event_id}
        self._acknowledged_path.parent.mkdir(parents=True, exist_ok=True)
        self._acknowledged_path.write_text(json.dumps(sorted(ids)), encoding="utf-8")

    def _acknowledged(self) -> set[str]:
        # Kept beside the env files rather than in the cache, so a rebuild doesn't resurface old conflicts.
        if self._acknowledged_path is None or not self._acknowledged_path.is_file():
            return set()
        try:
            return set(json.loads(self._acknowledged_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return set()

    # --- commands --------------------------------------------------------

    def create_request(self, workflow_key: str, title: str) -> str:
        try:
            defn = self.registry.get(workflow_key)
        except KeyError:
            raise CommandError(f"Unknown workflow {workflow_key!r}") from None
        if not defn.runnable_by(__version__):
            raise CommandError(f"{defn.title} (version {defn.version}) needs WFT {defn.min_app_version} or later; "
                               f"this is {__version__}. Run wft-update to upgrade.")
        request_id = new_id()
        with self._lock:
            self._record(request_id, defn, engine.create_request(defn, title))
        return request_id

    # --- team, ownership and tasks ------------------------------------------

    @property
    def team(self) -> Team:
        return self.team_source.team if self.team_source is not None else Team()

    @property
    def is_admin(self) -> bool:
        return self.team.is_admin(self.user)

    def people(self) -> list[tuple[str, str]]:
        """Everyone a request or step could be given to: the team plus anyone who has used WFT."""
        seen = {self.user}
        for r in self.rm.list_requests():
            seen |= {r.owner, r.created_by} | {s.assignee for s in r.steps if s.assignee}
        return self.team.choices(seen)

    def change_owner(self, request_id: str, owner: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.change_owner(state, owner))

    def claim_step(self, request_id: str, step_id: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.claim_step(state, defn, step_id, self.user))

    def assign_step(self, request_id: str, step_id: str, user: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.assign_step(state, defn, step_id, user))

    def release_step(self, request_id: str, step_id: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.release_step(state, defn, step_id))

    def tasks(self, user: str | None = None) -> Tasks:
        user = (user or self.user).lower()
        mine, on_mine, claimable, drafts, flagged = [], [], [], [], []
        for r in self.rm.list_requests():
            if r.owner == user and any(f.open for f in r.flags):
                flagged.append(r)
            if r.status == engine.DRAFT:
                if r.owner == user:
                    drafts.append(r)
                continue
            if r.status != engine.ACTIVE:
                continue
            try:
                defn = self.registry.get(r.workflow)
            except KeyError:
                continue
            for st in r.steps:
                if st.status not in engine.OPEN_STEP:
                    continue
                task = Task(r, st.id, defn.step(st.id).title, defn.title)
                if st.assignee == user:
                    mine.append(task)
                elif st.assignee:
                    continue
                elif r.owner == user:
                    on_mine.append(task)
                else:
                    claimable.append(task)
        oldest = lambda t: t.request.created_at  # noqa: E731
        return Tasks(sorted(mine, key=oldest), sorted(on_mine, key=oldest), sorted(claimable, key=oldest),
                     sorted(drafts, key=lambda r: r.created_at), sorted(flagged, key=lambda r: r.created_at))

    def open_flags(self) -> list[OpenFlag]:
        """Every unresolved or deferred impact flag, oldest first."""
        out = []
        for r in self.rm.list_requests():
            if not r.flags:
                continue
            try:
                defn = self.registry.get(r.workflow)
            except KeyError:
                defn = None
            for f in r.flags:
                if f.open:
                    title = defn.step(f.step).title if defn and f.step else None
                    out.append(OpenFlag(r, f, title))
        return sorted(out, key=lambda o: (o.flag.resolution == engine.DEFERRED, o.flag.raised_at))

    def follow_ups(self, request_id: str) -> list[RequestState]:
        return [r for r in self.rm.list_requests() if r.follow_up_of == request_id]

    def amend_preview(self, request_id: str, forms: dict[str, dict]) -> engine.Impact:
        """Validate an amendment and say what it would do, without recording anything."""
        state, defn = self._load(request_id)
        event = engine.amend_request(state, defn, forms)  # validates; raises if nothing changed
        new_inputs = {k: v for data in event.payload["forms"].values() for k, v in data.items()}
        return engine.amend_impact(state, defn, new_inputs)

    def amend(self, request_id: str, forms: dict[str, dict]) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.amend_request(state, defn, forms))

    # --- trading systems -------------------------------------------------------

    def _adapter(self, operation: str) -> tuple[Adapter, str]:
        system, op = split_operation(operation)
        adapter = self.adapters.get(system)
        if adapter is None:
            raise AdapterUnavailable(f"{system.upper()} isn't configured for this environment")
        return adapter, op

    def picklist(self, name: str, force: bool = False) -> Picklist:
        """Suggestions for a form field (`x-picklist`), cached locally. Never raises: falls back to typing."""
        try:
            adapter, op = self._adapter(name)
        except AdapterUnavailable as e:
            return Picklist([], None, True, str(e))
        if self.picklists is None:
            try:
                return Picklist(adapter.list(op), None, False)
            except AdapterUnavailable as e:
                return Picklist([], None, True, str(e))
        return self.picklists.get(name, lambda: adapter.list(op), force=force)

    def refresh_picklists(self, names: list[str]) -> dict[str, Picklist]:
        return {n: self.picklist(n, force=True) for n in names}

    def verify_check(self, request_id: str, step_id: str) -> dict:
        """Look the step's item up in the trading system, compare with what's expected, and record the result."""
        state, defn = self._load(request_id)
        operation, key, expected = engine.verify_target(state, defn, step_id)
        engine._verify_step(state, defn, step_id)  # fail fast before calling the system
        check = {"operation": operation, "key": key, "expected": expected}
        try:
            adapter, op = self._adapter(operation)
            fetched = adapter.fetch(op, key)
            differences = compare(expected, fetched.values)
            check.update(outcome=engine.MISMATCH if differences else engine.MATCH, actual=fetched.values,
                         differences=differences, source=fetched.source, as_of=fetched.as_of)
        except NotFoundInSystem as e:
            check.update(outcome=engine.MISMATCH, actual=None, error=str(e),
                         differences=[{"field": "(item)", "expected": key, "actual": "not found"}])
        except AdapterUnavailable as e:
            check.update(outcome=engine.UNAVAILABLE, error=str(e))
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.record_check(state, defn, step_id, check))
        return check

    def accept_verification(self, request_id: str, step_id: str, notes: str = "") -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.accept_verification(state, defn, step_id, notes))

    def complete_verification_manually(self, request_id: str, step_id: str, notes: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.complete_verification_manually(state, defn, step_id, notes))

    def prefill(self, request_id: str, step_id: str, data: dict) -> dict:
        """Fill a form's `prefill.target` from the trading system, keyed by `prefill.key` in the form data.

        Saves the result as the form's draft and returns it. Raises AdapterUnavailable / NotFoundInSystem.
        """
        state, defn = self._load(request_id)
        spec = defn.step(step_id).raw.get("prefill")
        if not spec:
            raise CommandError("This form has nothing to fill from a trading system")
        key = data
        for part in spec["key"].split("."):
            key = key.get(part) if isinstance(key, dict) else None
        if key in (None, ""):
            raise CommandError(f"Fill in {spec['key']} first")
        adapter, op = self._adapter(spec["operation"])
        fetched = adapter.fetch(op, key)
        filled = {**data, spec["target"]: fetched.values}
        self.save_form(request_id, step_id, filled)
        return filled

    # --- done outside WFT -------------------------------------------------------

    def complete_outside_tool(self, request_id: str, step_id: str, notes: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.complete_outside_tool(state, defn, step_id, notes))

    @property
    def reasons(self) -> tuple[Reason, ...]:
        return self.reasons_source.value if self.reasons_source is not None else DEFAULT_REASONS

    def reason_label(self, reason_id: str) -> str:
        return next((r.label for r in self.reasons if r.id == reason_id), reason_id)

    def record_off_process(self, system: str, what: str, performed_on: str, reason: str, notes: str,
                           linked_request: str = "") -> str:
        """Log a setup done entirely outside WFT. Returns the record id."""
        linked = None
        if linked_request.strip():
            found = self._find(linked_request)
            if found is None:
                raise CommandError(f"No request {linked_request.strip()!r} to link")
            linked = found.request_id
        today = timings.now_utc().date()
        new = offprocess.record(system, what, performed_on, reason, notes, linked, [r.id for r in self.reasons], today)
        record_id = new_id()
        with self._lock:
            self._record(record_id, offprocess.DEFINITION, new)
        self._assign_off_process_reference(record_id)
        return record_id

    def _assign_off_process_reference(self, record_id: str) -> None:
        if self.references is None:
            return
        with self._lock:
            rec = self.get_off_process(record_id)
            if rec.reference:
                return
            try:
                reference = self.references.allocate(offprocess.REFERENCE_PREFIX, int(rec.recorded_at[:4]))
            except ReferenceUnavailable as e:
                log.warning("No reference for off-process record %s yet: %s", record_id, e)
                return
            try:
                self._record(record_id, offprocess.DEFINITION, offprocess.assign_reference(rec, reference))
            except ReadOnly:
                log.warning("Allocated %s but couldn't record it", reference)

    def get_off_process(self, record_id: str) -> offprocess.OffProcessRecord:
        rec = self.rm.get_off_process(record_id)
        if rec is None:
            raise NotFound(f"off-process record {record_id}")
        return rec

    def off_process_records(self, q: str = "", reason: str = "", system: str = "") -> list[offprocess.OffProcessRecord]:
        q = q.strip().lower()
        out = []
        for rec in self.rm.list_off_process():
            if reason and rec.reason != reason:
                continue
            if system and rec.system.lower() != system.lower():
                continue
            if q and not any(q in (field or "").lower() for field in
                             (rec.reference, rec.system, rec.what, rec.notes, self.reason_label(rec.reason),
                              *(n.text for n in rec.extra_notes))):
                continue
            out.append(rec)
        return out

    def off_process_for_request(self, request_id: str) -> list[offprocess.OffProcessRecord]:
        return [r for r in self.rm.list_off_process() if r.linked_request == request_id]

    def add_off_process_note(self, record_id: str, text: str) -> None:
        with self._lock:
            rec = self.get_off_process(record_id)
            self._record(record_id, offprocess.DEFINITION, offprocess.add_note(rec, text))

    def add_off_process_attachment(self, record_id: str, name: str, stream) -> str:
        if self.attachments is None:
            raise CommandError("Attachments aren't available")
        if self._read_only_reason is not None:
            raise ReadOnly("The share is unavailable, so files can't be attached until it's back")
        self.get_off_process(record_id)
        try:
            stored = self.attachments.put(record_id, name, stream)
        except AttachmentTooLarge as e:
            raise FileTooLarge(str(e)) from e
        except OSError as e:
            self._go_read_only(e)
            raise ReadOnly("The share became unavailable; the file was not attached") from e
        with self._lock:
            rec = self.get_off_process(record_id)
            self._record(record_id, offprocess.DEFINITION,
                         offprocess.add_attachment(rec, name, stored.size, stored.sha256, stored.path))
        return stored.sha256

    def off_process_attachment_file(self, record_id: str, sha256: str):
        rec = self.get_off_process(record_id)
        try:
            att = rec.attachment(sha256)
        except KeyError:
            raise NotFound(f"off-process record {record_id} attachment {sha256[:12]}") from None
        try:
            return att, self.attachments.open_path(att.path)
        except FileNotFoundError:
            raise CommandError(f"{att.name} isn't on the shared drive (is it reachable?)") from None

    def hold(self, request_id: str, reason: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.hold_request(state, defn, reason))

    def resume(self, request_id: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.resume_request(state, defn))

    def cancel(self, request_id: str, reason: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.cancel_request(state, defn, reason))

    def resolve_flag(self, request_id: str, flag_id: str, resolution: str, notes: str = "",
                     linked_request: str | None = None) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            if linked_request:
                linked = self._find(linked_request)
                if linked is None:
                    raise CommandError(f"No request {linked_request!r} to link")
                linked_request = linked.request_id
            self._record(request_id, defn, engine.resolve_flag(state, flag_id, resolution, notes, linked_request))

    def create_follow_up(self, request_id: str, title: str | None = None) -> str:
        """A new request (latest version of the same workflow) linked to a finished one."""
        state, defn = self._load(request_id)
        if state.status not in engine.TERMINAL:
            raise CommandError("Follow-up requests are for completed or cancelled requests; change this one directly")
        latest = next(w for w in self.registry.latest() if w.id == defn.id)
        title = (title or "").strip() or f"Follow-up to {state.reference or state.title}"
        if not latest.runnable_by(__version__):
            raise CommandError(f"{latest.title} needs WFT {latest.min_app_version} or later")
        request_id_new = new_id()
        with self._lock:
            self._record(request_id_new, latest, engine.create_request(latest, title, follow_up_of=request_id))
        return request_id_new

    def _find(self, ref_or_id: str) -> RequestState | None:
        ref_or_id = ref_or_id.strip()
        state = self.rm.get(ref_or_id)
        if state:
            return state
        return next((r for r in self.rm.list_requests() if (r.reference or "").lower() == ref_or_id.lower()), None)

    def start_step(self, request_id: str, step_id: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.start_step(state, defn, step_id))

    def wait_step(self, request_id: str, step_id: str, reason: str, note: str = "") -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.wait_step(state, defn, step_id, reason, note))

    def resume_step(self, request_id: str, step_id: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.resume_step(state, defn, step_id))

    # --- views ---------------------------------------------------------------

    def open_requests(self, status: str = "open", workflow: str = "", owner: str = "", q: str = "",
                      sort: str = "age", descending: bool = True, now=None) -> list[Row]:
        now = now or timings.now_utc()
        wanted = STATUS_FILTERS.get(status, STATUS_FILTERS["open"])
        q = q.strip().lower()
        rows = []
        for r in self.rm.list_requests():
            if wanted is not None and r.status not in wanted:
                continue
            if owner and r.owner != owner.lower():
                continue
            try:
                defn = self.registry.get(r.workflow)
            except KeyError:
                continue
            if workflow and defn.id != workflow:
                continue
            if q and q not in r.title.lower() and q not in (r.reference or "").lower():
                continue
            current = next((s for s in r.steps if s.id == r.current_step), None)
            rows.append(Row(r, defn.title, defn.step(current.id).title if current else None,
                            current.status if current else None,
                            timings.request_age(r, now), timings.time_in_current_step(r, now)))
        return sorted(rows, key=SORT_KEYS.get(sort, SORT_KEYS["age"]), reverse=descending)

    def waiting_board(self, now=None) -> dict[str, list[Waiting]]:
        """Waiting steps grouped by reason (in the order of engine.WAIT_REASONS), longest wait first."""
        now = now or timings.now_utc()
        board: dict[str, list[Waiting]] = {reason: [] for reason in engine.WAIT_REASONS}
        for r in self.rm.list_requests():
            if r.status != engine.ACTIVE:
                continue
            try:
                defn = self.registry.get(r.workflow)
            except KeyError:
                continue
            for st in r.steps:
                if st.status != engine.WAITING:
                    continue
                last = st.transitions[-1]
                board.setdefault(last.reason, []).append(Waiting(
                    r, st.id, defn.step(st.id).title, last.reason, last.note, last.ts, last.actor,
                    timings.seconds_between(last.ts, now)))
        for items in board.values():
            items.sort(key=lambda w: w.waited, reverse=True)
        return board

    def save_form(self, request_id: str, step_id: str, data: dict) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.save_form(state, defn, step_id, data))

    def submit(self, request_id: str, forms: dict[str, dict]) -> None:
        """Validate the initial form(s), make the request Active, and give it a readable reference."""
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.submit_request(state, defn, forms))
        self._assign_reference(request_id)

    def complete_form(self, request_id: str, step_id: str, data: dict) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.complete_form(state, defn, step_id, data))

    def add_attachment(self, request_id: str, name: str, stream, step_id: str | None = None) -> str:
        """Store a file write-once on the share and record it on the request. Returns its SHA-256."""
        if self.attachments is None:
            raise CommandError("Attachments aren't available")
        if self._read_only_reason is not None:
            raise ReadOnly("The share is unavailable, so files can't be attached until it's back")
        _, defn = self._load(request_id)
        engine.check_step_exists(defn, step_id)  # before spending time uploading
        try:
            stored = self.attachments.put(request_id, name, stream)  # slow over the network: not under the lock
        except AttachmentTooLarge as e:
            raise FileTooLarge(str(e)) from e
        except OSError as e:
            self._go_read_only(e)
            raise ReadOnly("The share became unavailable; the file was not attached") from e
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.add_attachment(
                state, defn, name, stored.size, stored.sha256, stored.path, step_id))
        return stored.sha256

    def attachment_file(self, request_id: str, sha256: str):
        """(attachment, path on the share) for downloading."""
        state, _ = self._load(request_id)
        try:
            att = state.attachment(sha256)
        except KeyError:
            raise NotFound(f"{request_id} attachment {sha256[:12]}") from None
        if self.attachments is None:
            raise CommandError("Attachments aren't available")
        try:
            return att, self.attachments.open_path(att.path)
        except FileNotFoundError:
            raise CommandError(f"{att.name} isn't on the shared drive (is it reachable?)") from None

    def add_note(self, request_id: str, text: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.add_note(state, text))

    def set_checklist_item(self, request_id: str, step_id: str, index: int, checked: bool) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.set_checklist_item(state, defn, step_id, index, checked))

    def complete_step(self, request_id: str, step_id: str) -> None:
        with self._lock:
            state, defn = self._load(request_id)
            self._record(request_id, defn, engine.complete_step(state, defn, step_id))

    def get(self, request_id: str) -> tuple[RequestState, WorkflowDef]:
        return self._load(request_id)

    def _load(self, request_id: str) -> tuple[RequestState, WorkflowDef]:
        state = self.rm.get(request_id)
        if state is None:
            raise NotFound(request_id)
        return state, self.registry.get(state.workflow)

    def _record(self, request_id: str, defn: WorkflowDef | str, new: NewEvent) -> None:
        """Stamp and store an event. `defn` is the workflow definition, or a definition key (off-process records)."""
        if self._read_only_reason is not None:
            raise ReadOnly("The share is unavailable, so changes are blocked until it's back")
        event = Event(
            event_id=new_id(), request_id=request_id, seq=new.seq, type=new.type,
            actor=self.user, host=self.host, app_version=__version__,
            definition=defn if isinstance(defn, str) else defn.key, ts=utc_now(), payload=new.payload, prev=new.prev,
        )
        try:
            self.repo.append(event)
        except OSError as e:
            self._go_read_only(e)
            raise ReadOnly("The share became unavailable; your change was not saved") from e
        self.rm.ingest([event])
