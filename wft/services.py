"""Application services: run engine commands, stamp and persist events, update the read model."""
from __future__ import annotations

import threading

from wft import __version__, engine
from wft.definitions import DefinitionRegistry, WorkflowDef
from wft.engine import CommandError, NewEvent, RequestState
from wft.events import Event, new_id, utc_now
from wft.readmodel import ReadModel
from wft.repository import Repository


class NotFound(Exception):
    pass


class Services:
    def __init__(self, repo: Repository, read_model: ReadModel, registry: DefinitionRegistry,
                 user: str, host: str):
        self.repo = repo
        self.rm = read_model
        self.registry = registry
        self.user = user
        self.host = host
        self._lock = threading.Lock()

    def sync(self) -> None:
        """Bring the read model up to date with every journal on the share."""
        self.rm.ingest(self.repo.read_all())

    def create_request(self, workflow_key: str, title: str) -> str:
        try:
            defn = self.registry.get(workflow_key)
        except KeyError:
            raise CommandError(f"Unknown workflow {workflow_key!r}") from None
        request_id = new_id()
        with self._lock:
            self._record(request_id, defn, engine.create_request(defn, title))
        return request_id

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
        event = Event(
            event_id=new_id(), request_id=request_id, seq=new.seq, type=new.type,
            actor=self.user, host=self.host, app_version=__version__,
            definition=defn.key, ts=utc_now(), payload=new.payload,
        )
        self.repo.append(event)
        self.rm.ingest([event])
