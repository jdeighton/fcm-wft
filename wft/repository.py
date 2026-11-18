"""Event storage on the shared drive.

Layout under the share root:

    journals/YYYY-MM/<user>.jsonl     each user's instance is the only writer of its own files
    instance-locks/<user>.lock        one running instance per user

All storage access goes through `Repository` so the backend can be swapped later.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from wft.events import Event, utc_now

log = logging.getLogger(__name__)


Cursor = dict[str, int]  # journal (relative path) -> bytes consumed up to the last complete line


class ShareUnavailable(OSError):
    """The share can't be reached; the instance should go read-only."""


class Repository(Protocol):
    def append(self, event: Event) -> None: ...
    def read_since(self, cursor: Cursor) -> tuple[list[Event], Cursor]: ...


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

    def _check_reachable(self) -> None:
        try:
            ok = self.root.is_dir()
        except OSError as e:
            raise ShareUnavailable(f"Share not reachable: {self.root} ({e})") from e
        if not ok:
            raise ShareUnavailable(f"Share not reachable: {self.root}")

    def append(self, event: Event) -> None:
        """Append and flush to the share; the event is durable when this returns."""
        self._check_reachable()  # never recreate a missing share root by accident
        path = self.journal_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "ab") as f:
            if f.tell() and _last_byte(path) != b"\n":
                f.write(b"\n")  # an earlier write was cut off; don't glue this event onto the fragment
            f.write(event.to_line().encode("utf-8") + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def journal_files(self) -> list[Path]:
        return sorted((self.root / "journals").glob("*/*.jsonl"))

    def read_since(self, cursor: Cursor) -> tuple[list[Event], Cursor]:
        """Read complete lines appended since `cursor`; returns the events and the new cursor.

        Change detection is by file size (journals only ever grow); a trailing
        partial line is left for the next read.
        """
        self._check_reachable()
        events: list[Event] = []
        new_cursor = dict(cursor)
        journals = self.root / "journals"
        for path in self.journal_files():
            key = path.relative_to(journals).as_posix()
            offset = cursor.get(key, 0)
            size = path.stat().st_size
            if size == offset:
                continue
            if size < offset:
                offset = 0  # file replaced (shouldn't happen); re-read, duplicates are ignored downstream
            with open(path, "rb") as f:
                f.seek(offset)
                data = f.read()
            complete, newline, _partial = data.rpartition(b"\n")
            if not newline:
                continue  # only a partial line so far
            events.extend(_parse(complete))
            new_cursor[key] = offset + len(complete) + 1
        return events, new_cursor

    def read_all(self) -> list[Event]:
        return self.read_since({})[0]


def read_journal(path: Path) -> Iterator[Event]:
    """Yield complete events; a trailing line without a newline is still being written and is skipped."""
    complete, _, _partial = path.read_bytes().rpartition(b"\n")
    yield from _parse(complete)


def _parse(complete: bytes) -> Iterator[Event]:
    for line in complete.split(b"\n"):
        if not line.strip():
            continue
        try:
            yield Event.from_line(line.decode("utf-8"))
        except (ValueError, TypeError) as e:
            # A fragment left by a write that was cut off (e.g. the network dropped mid-append).
            log.warning("Skipping unreadable journal line (%s): %.80r", e, line)


def _last_byte(path: Path) -> bytes:
    with open(path, "rb") as f:
        f.seek(-1, os.SEEK_END)
        return f.read(1)


def default_identity() -> tuple[str, str]:
    """(Windows username, host name) for this instance."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or "unknown"
    return user.lower(), socket.gethostname()


class ReferenceUnavailable(Exception):
    """A readable reference couldn't be allocated right now (lock busy or share unavailable)."""


class ReferenceAllocator:
    """Hands out readable references `PREFIX-YYYY-NNNN` from counters on the share.

    Each prefix/year has a counter file under `meta/references/`, updated while
    holding a short-lived lock file created with O_EXCL. A crash after bumping
    the counter can skip a number, but a number is never handed out twice.
    """

    def __init__(self, share_root: Path, wait: float = 5.0, stale_after: float = 30.0):
        self.root = share_root
        self.wait = wait
        self.stale_after = stale_after

    def allocate(self, prefix: str, year: int) -> str:
        if not self.root.is_dir():
            raise ReferenceUnavailable(f"Share not reachable: {self.root}")
        lock = self.root / "locks" / f"reference-{prefix}-{year}.lock"
        counter = self.root / "meta" / "references" / f"{prefix}-{year}.txt"
        lock.parent.mkdir(parents=True, exist_ok=True)
        counter.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime > self.stale_after:
                        lock.unlink(missing_ok=True)  # left behind by a crash mid-allocation
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise ReferenceUnavailable(f"Reference counter {prefix}-{year} is busy; will retry") from None
                time.sleep(0.05)
            except OSError as e:
                raise ReferenceUnavailable(f"Can't lock reference counter: {e}") from e
        try:
            os.close(fd)
            try:
                last = int(counter.read_text(encoding="utf-8").strip() or 0)
            except FileNotFoundError:
                last = 0
            number = last + 1
            tmp = counter.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(str(number), encoding="utf-8")
            os.replace(tmp, counter)
            return f"{prefix}-{year}-{number:04d}"
        except (OSError, ValueError) as e:
            raise ReferenceUnavailable(f"Can't update reference counter {counter}: {e}") from e
        finally:
            lock.unlink(missing_ok=True)


class AttachmentTooLarge(Exception):
    pass


@dataclass(frozen=True)
class StoredFile:
    path: str   # relative to the share root, with forward slashes
    size: int
    sha256: str


_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str, limit: int = 80) -> str:
    """A name that's safe on any file system; keeps the extension. The original name is kept in the journal."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = _UNSAFE.sub("_", stem).strip("._") or "file"
    ext = _UNSAFE.sub("", ext)[:10]
    return f"{stem[: limit - len(ext) - 1]}.{ext}" if ext else stem[:limit]


class AttachmentStore:
    """Write-once file storage under `attachments/<request id>/` on the share.

    Uploads are streamed to a temporary file in the target folder (hashing as
    they go), then renamed into place. Stored files are never overwritten: the
    name includes a hash prefix, so the same name means the same content.
    """

    CHUNK = 1024 * 1024

    def __init__(self, share_root: Path, max_bytes: int):
        self.root = share_root
        self.max_bytes = max_bytes

    def put(self, request_id: str, name: str, stream: BinaryIO) -> StoredFile:
        if not self.root.is_dir():
            raise ShareUnavailable(f"Share not reachable: {self.root}")
        if not re.fullmatch(r"[0-9a-f]{32}", request_id):
            raise ValueError("Invalid request id")
        folder = self.root / "attachments" / request_id
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / f".upload-{os.getpid()}-{time.monotonic_ns()}.tmp"
        digest, size = hashlib.sha256(), 0
        try:
            with open(tmp, "wb") as out:
                while chunk := stream.read(self.CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise AttachmentTooLarge(
                            f"{name} is larger than the {self.max_bytes // (1024 * 1024)} MB limit")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            sha = digest.hexdigest()
            final = folder / f"{sha[:16]}_{safe_filename(name)}"
            if final.exists():
                tmp.unlink()  # identical content already stored
            else:
                os.rename(tmp, final)
            return StoredFile(final.relative_to(self.root).as_posix(), size, sha)
        finally:
            tmp.unlink(missing_ok=True)

    def open_path(self, relative: str) -> Path:
        """The stored file for a journal-recorded path, refusing anything outside the attachments folder."""
        base = (self.root / "attachments").resolve()
        path = (self.root / relative).resolve()
        if base not in path.parents:
            raise ValueError("Attachment path outside the attachments folder")
        if not path.is_file():
            raise FileNotFoundError(relative)
        return path
