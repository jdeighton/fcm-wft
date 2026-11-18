"""Local SQLite read model: a disposable cache rebuilt from the journals.

Deleting the cache file is always safe; the next start re-ingests every journal.
"""
from __future__ import annotations

import json
from dataclasses import asdict
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from wft import engine
from wft.definitions import DefinitionRegistry
from wft.engine import Note, RequestState, StepState, Transition
from wft.events import Event

log = logging.getLogger(__name__)

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
CREATE TABLE IF NOT EXISTS conflicts (
    event_id   TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    actor      TEXT NOT NULL,
    line       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conflicts_by_actor ON conflicts (actor);
CREATE TABLE IF NOT EXISTS journal_cursor (
    journal TEXT PRIMARY KEY,
    offset  INTEGER NOT NULL
);
"""
TABLES = ("events", "requests", "conflicts", "journal_cursor")
# Bump when the cache layout or projected state changes; an older cache is dropped and rebuilt.
CACHE_VERSION = 6


class ReadModel:
    def __init__(self, path: Path | str, registry: DefinitionRegistry):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        if self._db.execute("PRAGMA user_version").fetchone()[0] != CACHE_VERSION:
            for table in TABLES:
                self._db.execute(f"DROP TABLE IF EXISTS {table}")
            self._db.execute(f"PRAGMA user_version = {CACHE_VERSION}")
        self._db.executescript(SCHEMA)
        self._lock = threading.Lock()
        self.registry = registry

    def ingest(self, events: Iterable[Event], cursor: dict[str, int] | None = None) -> set[str]:
        """Store events not seen before, re-project the requests they touch, and save the read cursor."""
        with self._lock, self._db:
            touched = set()
            for e in events:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO events (event_id, request_id, seq, ts, line) VALUES (?,?,?,?,?)",
                    (e.event_id, e.request_id, e.seq, e.ts, e.to_line()))
                if cur.rowcount:
                    touched.add(e.request_id)
            # Requests whose definition wasn't loaded yet when their events arrived get another try.
            unprojected = {r[0] for r in self._db.execute(
                "SELECT DISTINCT request_id FROM events WHERE request_id NOT IN (SELECT request_id FROM requests)")}
            for request_id in touched | unprojected:
                self._project(request_id)
            if cursor is not None:
                self._db.executemany("INSERT OR REPLACE INTO journal_cursor VALUES (?, ?)", cursor.items())
            return touched

    def _project(self, request_id: str) -> None:
        events = [Event.from_line(r[0]) for r in self._db.execute(
            "SELECT line FROM events WHERE request_id = ?", (request_id,))]
        created = next((e for e in events if e.seq == 1), events[0])
        try:
            defn = self.registry.get(created.definition)
        except KeyError:
            log.warning("Request %s uses definition %s, which isn't loaded yet", request_id, created.definition)
            return
        result = engine.replay(events, defn)
        state = result.state
        self._db.execute(
            "INSERT OR REPLACE INTO requests VALUES (?,?,?,?,?,?,?,?,?)",
            (state.request_id, state.workflow, state.title, state.status, state.created_by,
             state.created_at, state.updated_at, state.current_step, _dump_state(state)))
        self._db.execute("DELETE FROM conflicts WHERE request_id = ?", (request_id,))
        self._db.executemany(
            "INSERT INTO conflicts VALUES (?,?,?,?)",
            [(e.event_id, e.request_id, e.actor, e.to_line()) for e in result.losers])

    def cursor(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT journal, offset FROM journal_cursor").fetchall())

    def version(self) -> int:
        """Changes whenever new events are ingested; lets the UI notice updates cheaply."""
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def reset(self) -> None:
        """Empty the cache so the next sync re-reads every journal from the start."""
        with self._lock, self._db:
            for table in TABLES:
                self._db.execute(f"DELETE FROM {table}")

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
        """The winning chain of events plus notes and references, in time order."""
        with self._lock:
            rows = self._db.execute("SELECT line FROM events WHERE request_id = ?", (request_id,)).fetchall()
        events = [Event.from_line(r[0]) for r in rows]
        chain, _ = engine.resolve(events)
        return sorted(chain + engine.annotations(events), key=lambda e: (e.ts, e.seq, e.event_id))

    def conflicts_for(self, actor: str) -> list[Event]:
        """Actions by `actor` that were overridden by someone else's concurrent action."""
        with self._lock:
            rows = self._db.execute("SELECT line FROM conflicts WHERE actor = ?", (actor,)).fetchall()
        return sorted((Event.from_line(r[0]) for r in rows), key=lambda e: e.ts)

    def dump(self) -> list[tuple]:
        """All projected rows, for comparing a rebuilt cache with the original."""
        with self._lock:
            return self._db.execute("SELECT * FROM requests ORDER BY request_id").fetchall()

    def close(self) -> None:
        self._db.close()


def _dump_state(state: RequestState) -> str:
    d = asdict(state)
    for step in d["steps"]:
        step["ticked"] = sorted(step["ticked"])
    return json.dumps(d, sort_keys=True)


def _load_state(text: str) -> RequestState:
    d = json.loads(text)
    d["steps"] = tuple(StepState(**{**s, "ticked": frozenset(s["ticked"]),
                                    "transitions": tuple(Transition(**t) for t in s["transitions"])})
                       for s in d["steps"])
    d["notes"] = tuple(Note(**n) for n in d["notes"])
    return RequestState(**d)
