"""Local cache of picklists (accounts, users), so forms don't call the trading system on every page.

Kept per user under `<WFT_HOME>/cache/`, never on the share. Entries expire after a TTL and can be
refreshed by hand. When the system is unavailable, the last cached list is still offered (marked stale).
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from wft.adapters import AdapterUnavailable, Entity


@dataclass(frozen=True)
class Picklist:
    items: list[Entity]
    fetched_at: float | None   # epoch seconds; None if never fetched
    stale: bool                # past its TTL or the system couldn't be reached
    error: str | None = None   # why a fresh list couldn't be had


class PicklistCache:
    def __init__(self, path: Path, ttl_seconds: float):
        self.path = path
        self.ttl = ttl_seconds
        self._lock = threading.Lock()

    def get(self, name: str, loader, force: bool = False) -> Picklist:
        """The list `name` (e.g. `tt.accounts`), from cache if fresh, else from `loader()`."""
        with self._lock:
            entry = self._read().get(name)
            fresh = entry and time.time() - entry["fetched_at"] < self.ttl
            if fresh and not force:
                return Picklist(self._items(entry), entry["fetched_at"], False)
            try:
                items = loader()
            except AdapterUnavailable as e:
                if entry:
                    return Picklist(self._items(entry), entry["fetched_at"], True, str(e))
                return Picklist([], None, True, str(e))
            data = self._read()
            data[name] = {"fetched_at": time.time(),
                          "items": [{"id": i.id, "name": i.name, "extra": i.extra} for i in items]}
            self._write(data)
            return Picklist(items, data[name]["fetched_at"], False)

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)

    @staticmethod
    def _items(entry: dict) -> list[Entity]:
        return [Entity(i["id"], i["name"], i.get("extra") or {}) for i in entry["items"]]

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(self.path)
