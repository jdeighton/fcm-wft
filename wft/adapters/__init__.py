"""Trading-system adapters: read-only lookups for picklists, pre-filling forms and verify steps.

Each adapter exposes named *operations* that definitions refer to as `<system>.<operation>`:

- list operations (`accounts`, `users`) return `Entity` lists for picklists (`x-picklist` in a
  form schema);
- fetch operations (`account`, `user`, `account_limits`) return one item's values for a `verify`
  step or a form's `prefill`.

Where the data comes from is the adapter's business: the TT adapter calls the TT REST API; a later
file-drop adapter can read exported files instead (FR-7.5), reporting their export time as `as_of`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class AdapterUnavailable(Exception):
    """The trading system can't be reached (or isn't configured) right now."""


class NotFoundInSystem(Exception):
    """The trading system has no item with that key."""


@dataclass(frozen=True)
class Entity:
    id: str
    name: str
    extra: dict = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class Fetched:
    values: Any
    source: str          # e.g. "TT UAT REST API", "fake", or an export file name
    as_of: str | None = None  # when the data was current, for sources that aren't live


class Adapter(Protocol):
    system: str
    list_operations: frozenset[str]
    fetch_operations: frozenset[str]

    def list(self, operation: str) -> list[Entity]: ...
    def fetch(self, operation: str, key: Any) -> Fetched: ...


# Operations every adapter of a system must offer, so definitions can be validated without connecting.
KNOWN_OPERATIONS = {
    "tt": {"list": frozenset({"accounts", "users"}), "fetch": frozenset({"account", "user", "account_limits"})},
}


def split_operation(name: str) -> tuple[str, str]:
    system, _, operation = name.partition(".")
    return system, operation


def known_list_operation(name: str) -> bool:
    system, op = split_operation(name)
    return op in KNOWN_OPERATIONS.get(system, {}).get("list", ())


def known_fetch_operation(name: str) -> bool:
    system, op = split_operation(name)
    return op in KNOWN_OPERATIONS.get(system, {}).get("fetch", ())


def compare(expected: Any, actual: Any, prefix: str = "") -> list[dict]:
    """Differences between what a request expects and what the system reports.

    Only fields present in `expected` are compared (systems report much more). Numbers compare by
    value (10 == 10.0); strings ignore surrounding spaces and case.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [{"field": prefix.rstrip(".") or "(value)", "expected": expected, "actual": actual}]
        out = []
        for key, value in expected.items():
            out += compare(value, actual.get(key), f"{prefix}{key}.")
        return out
    if _same(expected, actual):
        return []
    return [{"field": prefix.rstrip(".") or "(value)", "expected": expected, "actual": actual}]


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b  # True must not equal 1
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    if isinstance(a, (int, float)) and isinstance(b, str):
        try:
            return float(a) == float(b)
        except ValueError:
            return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(sorted(a, key=str), sorted(b, key=str)))
    return a == b
