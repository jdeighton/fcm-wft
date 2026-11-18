"""Local SQLite read model: a disposable cache rebuilt from the journals.

Deleting the cache file is always safe; the next start re-ingests every journal.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from wft import engine
from wft.definitions import DefinitionRegistry
from wft.engine import RequestState, StepState
from wft.events import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id   TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    ts         TEXT NOT NULL,
    line       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_by_request ON events (request_id, seq);
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    workflow     TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    current_step TEXT,
    state_json   TEXT NOT NULL
);
"""


class ReadModel:
    def __init__(self, path: Path | str, registry: DefinitionRegistry):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.registry = registry

    def ingest(self, events: Iterable[Event]) -> set[str]:
        """Store events not seen before and re-project the requests they touch."""
        with self._lock, self._db:
            touched = set()
            for e in events:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO events (event_id, request_id, seq, ts, line) VALUES (?,?,?,?,?)",
                    (e.event_id, e.request_id, e.seq, e.ts, e.to_line()))
                if cur.rowcount:
                    touched.add(e.request_id)
            for request_id in touched:
                self._project(request_id)
            return touched

    def _project(self, request_id: str) -> None:
        events = [Event.from_line(r[0]) for r in self._db.execute(
            "SELECT line FROM events WHERE request_id = ? ORDER BY seq, ts, event_id", (request_id,))]
        defn = self.registry.get(events[0].definition)
        state = engine.replay(events, defn)
        self._db.execute(
            "INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
            (state.request_id, state.workflow, state.title, state.status, state.created_by,
             state.created_at, state.updated_at, state.current_step, _dump_state(state)))

    def get(self, request_id: str) -> RequestState | None:
        with self._lock:
            row = self._db.execute("SELECT state_json FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return _load_state(row[0]) if row else None

    def list_requests(self, status: str | None = None) -> list[RequestState]:
        sql, args = "SELECT state_json FROM requests", ()
        if status:
            sql, args = sql + " WHERE status = ?", (status,)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY created_at DESC", args).fetchall()
        return [_load_state(r[0]) for r in rows]

    def history(self, request_id: str) -> list[Event]:
        with self._lock:
            rows = self._db.execute(
                "SELECT line FROM events WHERE request_id = ? ORDER BY seq, ts, event_id", (request_id,)).fetchall()
        return [Event.from_line(r[0]) for r in rows]

    def dump(self) -> list[tuple]:
        """All projected rows, for comparing a rebuilt cache with the original."""
        with self._lock:
            return self._db.execute("SELECT * FROM requests ORDER BY request_id").fetchall()

    def close(self) -> None:
        self._db.close()


def _dump_state(state: RequestState) -> str:
    d = {k: getattr(state, k) for k in state.__dataclass_fields__ if k != "steps"}
    d["steps"] = [{**{k: getattr(s, k) for k in s.__dataclass_fields__}, "ticked": sorted(s.ticked)}
                  for s in state.steps]
    return json.dumps(d, sort_keys=True)


def _load_state(text: str) -> RequestState:
    d = json.loads(text)
    d["steps"] = tuple(StepState(**{**s, "ticked": frozenset(s["ticked"])}) for s in d["steps"])
    return RequestState(**d)
