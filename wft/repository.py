"""Event storage on the shared drive.

Layout under the share root:

    journals/YYYY-MM/<user>.jsonl     each user's instance is the only writer of its own files
    instance-locks/<user>.lock        one running instance per user

All storage access goes through `Repository` so the backend can be swapped later.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Protocol

from wft.events import Event, utc_now


class Repository(Protocol):
    def append(self, event: Event) -> None: ...
    def read_all(self) -> Iterator[Event]: ...


class InstanceLockError(Exception):
    pass


class InstanceLock:
    """Exclusive per-user lock file, created with O_EXCL and kept alive by a heartbeat.

    A lock whose heartbeat is older than `stale_after` seconds is treated as
    left behind by a crashed instance and taken over.
    """

    def __init__(self, path: Path, user: str, host: str,
                 heartbeat_every: float = 30.0, stale_after: float = 120.0):
        self.path = path
        self.user = user
        self.host = host
        self.heartbeat_every = heartbeat_every
        self.stale_after = stale_after
        self._started = utc_now()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if not self._is_stale():
                    raise InstanceLockError(self._held_message()) from None
                self.path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self._content())
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()
            return
        raise InstanceLockError(self._held_message())

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._owned():
            self.path.unlink(missing_ok=True)

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    def _content(self) -> str:
        return json.dumps({"user": self.user, "host": self.host, "pid": os.getpid(),
                           "started": self._started, "heartbeat": time.time()})

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_every):
            if self._owned():
                self.path.write_text(self._content(), encoding="utf-8")

    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _owned(self) -> bool:
        info = self._read()
        return bool(info) and info.get("host") == self.host and info.get("pid") == os.getpid()

    def _is_stale(self) -> bool:
        info = self._read()
        if info is None:
            # Unreadable or half-written: fall back to the file's modification time.
            try:
                return time.time() - self.path.stat().st_mtime > self.stale_after
            except FileNotFoundError:
                return True
        return time.time() - float(info.get("heartbeat", 0)) > self.stale_after

    def _held_message(self) -> str:
        info = self._read() or {}
        return (f"WFT is already running for {self.user} on {info.get('host', 'another machine')} "
                f"(pid {info.get('pid', '?')}, started {info.get('started', '?')}). "
                "Close it first, or wait a couple of minutes if it crashed.")


class FileRepository:
    def __init__(self, share_root: Path, user: str):
        self.root = share_root
        self.user = user

    def journal_path(self, when: datetime | None = None) -> Path:
        when = when or datetime.now(timezone.utc)
        return self.root / "journals" / when.strftime("%Y-%m") / f"{self.user}.jsonl"

    def append(self, event: Event) -> None:
        """Append and flush to the share; the event is durable when this returns."""
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(event.to_line() + "\n")
            f.flush()
            os.fsync(f.fileno())

    def journal_files(self) -> list[Path]:
        return sorted((self.root / "journals").glob("*/*.jsonl"))

    def read_all(self) -> Iterator[Event]:
        for path in self.journal_files():
            yield from read_journal(path)


def read_journal(path: Path) -> Iterator[Event]:
    """Yield complete events; a trailing line without a newline is still being written and is skipped."""
    data = path.read_bytes()
    complete, _, _partial = data.rpartition(b"\n")
    for line in complete.split(b"\n"):
        if line.strip():
            yield Event.from_line(line.decode("utf-8"))


def default_identity() -> tuple[str, str]:
    """(Windows username, host name) for this instance."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    return user.lower(), socket.gethostname()
