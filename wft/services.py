"""Application services: run engine commands, stamp and persist events, update the read model."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

from wft import __version__, engine
from wft.definitions import DefinitionRegistry, WorkflowDef
from wft.engine import CommandError, NewEvent, RequestState
from wft.events import Event, new_id, utc_now
from wft.readmodel import ReadModel
from wft.repository import ReferenceAllocator, ReferenceUnavailable, Repository, ShareUnavailable

log = logging.getLogger(__name__)

POLL_SECONDS = 10.0
DEFAULT_REFERENCE_PREFIX = "REQ"


class NotFound(Exception):
    pass


class ReadOnly(Exception):
    """The share is unavailable, so changes are blocked."""


@dataclass(frozen=True)
class Status:
    read_only: bool
    reason: str | None
    since: str | None
    version: int


class Services:
    def __init__(self, repo: Repository, read_model: ReadModel, registry: DefinitionRegistry,
                 user: str, host: str, acknowledged_path: Path | None = None,
                 references: ReferenceAllocator | None = None):
        self.repo = repo
        self.rm = read_model
        self.registry = registry
        self.user = user
        self.host = host
        self._acknowledged_path = acknowledged_path
        self.references = references
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

    def _record(self, request_id: str, defn: WorkflowDef, new: NewEvent) -> None:
        if self._read_only_reason is not None:
            raise ReadOnly("The share is unavailable, so changes are blocked until it's back")
        event = Event(
            event_id=new_id(), request_id=request_id, seq=new.seq, type=new.type,
            actor=self.user, host=self.host, app_version=__version__,
            definition=defn.key, ts=utc_now(), payload=new.payload, prev=new.prev,
        )
        try:
            self.repo.append(event)
        except OSError as e:
            self._go_read_only(e)
            raise ReadOnly("The share became unavailable; your change was not saved") from e
        self.rm.ingest([event])
