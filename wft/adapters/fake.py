"""An in-memory TT stand-in, for tests and for trying WFT without TT access (`WFT_TT_BASE_URL=fake:`)."""
from __future__ import annotations

import copy
from typing import Any

from wft.adapters import KNOWN_OPERATIONS, AdapterUnavailable, Entity, Fetched, NotFoundInSystem

SAMPLE = {
    "accounts": {
        "ACC1": {"id": "ACC1", "name": "ACC1 – Example Capital", "limits": {"max": 1000, "currency": "USD"}},
        "ACC7": {"id": "ACC7", "name": "ACC7 – Sample Fund", "limits": {"max": 2500, "currency": "USD"}},
        "ACC9": {"id": "ACC9", "name": "ACC9 – Demo Partners", "limits": {"max": 500, "currency": "EUR"}},
    },
    "users": {
        "jdoe": {"id": "jdoe", "name": "Jane Doe", "email": "jdoe@example.com", "accounts": ["ACC1"]},
        "rroe": {"id": "rroe", "name": "Richard Roe", "email": "rroe@example.com", "accounts": ["ACC7", "ACC9"]},
    },
}


class FakeTT:
    system = "tt"
    list_operations = KNOWN_OPERATIONS["tt"]["list"]
    fetch_operations = KNOWN_OPERATIONS["tt"]["fetch"]

    def __init__(self, data: dict | None = None):
        self.data = copy.deepcopy(data if data is not None else SAMPLE)
        self.available = True
        self.calls: list[tuple[str, Any]] = []

    def _check(self) -> None:
        if not self.available:
            raise AdapterUnavailable("TT (fake) is unavailable")

    def list(self, operation: str) -> list[Entity]:
        self._check()
        self.calls.append((operation, None))
        items = self.data[operation]
        return [Entity(k, v.get("name", k), {kk: vv for kk, vv in v.items() if kk not in ("id", "name")})
                for k, v in sorted(items.items())]

    def fetch(self, operation: str, key: Any) -> Fetched:
        self._check()
        self.calls.append((operation, key))
        table = "users" if operation == "user" else "accounts"
        item = self.data[table].get(str(key))
        if item is None:
            raise NotFoundInSystem(f"TT has no {table[:-1]} {key!r}")
        values = item["limits"] if operation == "account_limits" else item
        return Fetched(copy.deepcopy(values), source="fake TT")
