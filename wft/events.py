"""The event envelope stored as one JSON line in a user's journal."""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_SCHEMA_VERSION = 1


def new_id() -> str:
    """Time-ordered, collision-resistant id: 48-bit ms timestamp + 80 random bits, hex."""
    return f"{int(time.time() * 1000):012x}{os.urandom(10).hex()}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class Event:
    event_id: str
    request_id: str
    seq: int  # per-request sequence; this event follows seq - 1
    type: str
    actor: str
    host: str
    app_version: str
    definition: str  # "<workflow_id>@<version>"
    ts: str  # UTC ISO-8601
    payload: dict[str, Any] = field(default_factory=dict)
    prev: str | None = None  # event_id this event was decided on top of; None for the first event
    schema_version: int = EVENT_SCHEMA_VERSION

    def to_line(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"), sort_keys=True)

    @classmethod
    def from_line(cls, line: str) -> Event:
        return cls(**json.loads(line))
