"""Local SQLite read model: a disposable cache rebuilt from the journals.

Deleting the cache file is always safe; the next start re-ingests every journal.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from wft import engine, offprocess
from wft.definitions import DefinitionRegistry
from wft.engine import Attachment, Flag, Hold, Note, RequestState, StepState, Transition
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
CREATE TABLE IF NOT EXISTS off_process (
    record_id    TEXT PRIMARY KEY,
    performed_on TEXT NOT NULL,
    state_json   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    request_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    read       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS journal_cursor (
    journal TEXT PRIMARY KEY,
    offset  INTEGER NOT NULL
);
"""
TABLES = ("events", "requests", "conflicts", "off_process", "notices", "journal_cursor")
# Bump when the cache layout or projected state changes; an older cache is dropped and rebuilt.
CACHE_VERSION = 11


@dataclass(frozen=True)
class Change:
    request_id: str
    before: RequestState | None
    after: RequestState
    new_events: list[Event]


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

    def ingest(self, events: Iterable[Event], cursor: dict[str, int] | None = None,
               changes: list[Change] | None = None) -> set[str]:
        """Store events not seen before, re-project the requests they touch, and save the read cursor.

        If `changes` is given, a Change (state before and after, and the new events) is appended for
        each request touched, so callers can tell people what changed.
        """
        with self._lock, self._db:
            touched = set()
            new_events: dict[str, list[Event]] = {}
            for e in events:
                cur = self._db.execute(
                    "INSERT OR IGNORE INTO events (event_id, request_id, seq, ts, line) VALUES (?,?,?,?,?)",
                    (e.event_id, e.request_id, e.seq, e.ts, e.to_line()))
                if cur.rowcount:
                    touched.add(e.request_id)
                    new_events.setdefault(e.request_id, []).append(e)
            before = {rid: self._get(rid) for rid in touched} if changes is not None else {}
            # Requests whose definition wasn't loaded yet when their events arrived get another try.
            unprojected = {r[0] for r in self._db.execute(
                "SELECT DISTINCT request_id FROM events WHERE request_id NOT IN (SELECT request_id FROM requests)"
                " AND request_id NOT IN (SELECT record_id FROM off_process)")}
            for request_id in touched | unprojected:
                self._project(request_id)
            if changes is not None:
                for rid in touched:
                    after = self._get(rid)
                    if after is not None:
                        changes.append(Change(rid, before.get(rid), after, new_events.get(rid, [])))
            if cursor is not None:
                self._db.executemany("INSERT OR REPLACE INTO journal_cursor VALUES (?, ?)", cursor.items())
            return touched

    def _project(self, request_id: str) -> None:
        events = [Event.from_line(r[0]) for r in self._db.execute(
            "SELECT line FROM events WHERE request_id = ?", (request_id,))]
        created = next((e for e in events if e.seq == 1), events[0])
        if created.definition == offprocess.DEFINITION:
            if created.seq == 1:  # otherwise the record itself hasn't been read yet
                rec = offprocess.replay(events)
                self._db.execute("INSERT OR REPLACE INTO off_process VALUES (?,?,?)",
                                 (rec.record_id, rec.performed_on, json.dumps(asdict(rec), sort_keys=True)))
            return
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

    def list_off_process(self) -> list[offprocess.OffProcessRecord]:
        with self._lock:
            rows = self._db.execute("SELECT state_json FROM off_process ORDER BY performed_on DESC, record_id DESC").fetchall()
        return [_load_off_process(r[0]) for r in rows]

    def get_off_process(self, record_id: str) -> offprocess.OffProcessRecord | None:
        with self._lock:
            row = self._db.execute("SELECT state_json FROM off_process WHERE record_id = ?", (record_id,)).fetchone()
        return _load_off_process(row[0]) if row else None

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
            return self._get(request_id)

    def _get(self, request_id: str) -> RequestState | None:
        row = self._db.execute("SELECT state_json FROM requests WHERE request_id = ?", (request_id,)).fetchone()
        return _load_state(row[0]) if row else None

    # --- notices (per user, local) ---------------------------------------------

    def add_notices(self, notices) -> None:
        with self._lock, self._db:
            self._db.executemany("INSERT INTO notices (ts, request_id, kind, title, text) VALUES (?,?,?,?,?)",
                                 [(n.ts, n.request_id, n.kind, n.title, n.text) for n in notices])

    def notices(self, unread_only: bool = False, limit: int = 200, after_id: int = 0) -> list[dict]:
        sql = "SELECT id, ts, request_id, kind, title, text, read FROM notices WHERE id > ?"
        if unread_only:
            sql += " AND read = 0"
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id DESC LIMIT ?", (after_id, limit)).fetchall()
        keys = ("id", "ts", "request_id", "kind", "title", "text", "read")
        return [dict(zip(keys, r)) for r in rows]

    def unread_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM notices WHERE read = 0").fetchone()[0]

    def mark_read(self, request_id: str | None = None) -> None:
        with self._lock, self._db:
            if request_id:
                self._db.execute("UPDATE notices SET read = 1 WHERE request_id = ?", (request_id,))
            else:
                self._db.execute("UPDATE notices SET read = 1")

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
    d["attachments"] = tuple(Attachment(**a) for a in d["attachments"])
    d["holds"] = tuple(Hold(**h) for h in d["holds"])
    d["flags"] = tuple(Flag(**f) for f in d["flags"])
    return RequestState(**d)


def _load_off_process(text: str) -> offprocess.OffProcessRecord:
    d = json.loads(text)
    d["attachments"] = tuple(Attachment(**a) for a in d["attachments"])
    d["extra_notes"] = tuple(Note(**n) for n in d["extra_notes"])
    return offprocess.OffProcessRecord(**d)
