"""Small published configuration files (besides workflow definitions), reloaded when they change.

Each lives in the repo under `definitions/config/<name>` and is published to
`<share>/definitions/config/<name>` by scripts/publish_definitions.py.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

import jsonschema
import yaml

from wft.team import roles_errors

log = logging.getLogger(__name__)
T = TypeVar("T")

SHARE_CONFIG_DIR = Path("definitions") / "config"
REPO_CONFIG_DIR = Path("config")
REASONS_NAME = "off_process_reasons.yaml"


# --- off-process reason categories ----------------------------------------------

@dataclass(frozen=True)
class Reason:
    id: str
    label: str


DEFAULT_REASONS = (
    Reason("urgent", "Urgent / out of hours"),
    Reason("unusual_structure", "Unusual structure not supported"),
    Reason("tool_gap", "Tool missing workflow or step"),
    Reason("tool_unavailable", "Tool or share unavailable"),
    Reason("trading_system_issue", "Trading-system issue"),
    Reason("other", "Other (notes required)"),
)

REASONS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reasons"],
    "properties": {"reasons": {"type": "array", "minItems": 1, "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "label"],
        "properties": {"id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}, "label": {"type": "string", "minLength": 1}},
    }}},
}


def reasons_errors(data: Any) -> list[str]:
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or REASONS_NAME}: {e.message}"
              for e in jsonschema.Draft202012Validator(REASONS_SCHEMA).iter_errors(data)]
    if not errors:
        ids = [r["id"] for r in data["reasons"]]
        errors += [f"reason id {i!r} is listed more than once" for i in sorted({i for i in ids if ids.count(i) > 1})]
    return errors


def parse_reasons(data: Any) -> tuple[Reason, ...]:
    errors = reasons_errors(data)
    if errors:
        raise ValueError("; ".join(errors))
    return tuple(Reason(r["id"], r["label"]) for r in data["reasons"])


# Every published config file and how to check it.
CONFIG_FILES: dict[str, Callable[[Any], list[str]]] = {
    "roles.yaml": roles_errors,
    REASONS_NAME: reasons_errors,
}


class ReloadingFile(Generic[T]):
    """A parsed YAML file that reloads when it changes; a missing file gives `default`, a broken one keeps the last good value."""

    def __init__(self, path: Path, parse: Callable[[Any], T], default: T):
        self.path = path
        self._parse = parse
        self._value = default
        self._default = default
        self._stamp: tuple[float, int] | None = None
        self._lock = threading.Lock()
        self.refresh()

    @property
    def value(self) -> T:
        with self._lock:
            return self._value

    def refresh(self) -> bool:
        try:
            st = self.path.stat()
            stamp = (st.st_mtime, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return False
        try:
            value = self._default if stamp is None else self._parse(yaml.safe_load(self.path.read_text(encoding="utf-8")))
        except (ValueError, OSError, yaml.YAMLError) as e:
            log.error("Ignoring invalid %s: %s", self.path, e)
            return False
        with self._lock:
            self._value, self._stamp = value, stamp
        return True
