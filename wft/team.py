"""Who's on the team and what roles they have, from published configuration.

`definitions/config/roles.yaml` in the repo is published to the same path on the
share. Everyone who runs WFT is an Operator; users listed with the `admin` role
are also Admins. Identity is the Windows username, compared case-insensitively.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import yaml

log = logging.getLogger(__name__)

ROLES_FILE = Path("definitions") / "config" / "roles.yaml"  # relative to the share root
REPO_ROLES_FILE = Path("config") / "roles.yaml"            # relative to the repo's definitions folder
ADMIN = "admin"
OPERATOR = "operator"

ROLES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["users"],
    "properties": {
        "users": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["username"],
                "properties": {
                    "username": {"type": "string", "pattern": "^[A-Za-z0-9._$-]+$"},
                    "name": {"type": "string"},
                    "roles": {"type": "array", "items": {"enum": [ADMIN]}, "uniqueItems": True},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class Member:
    username: str
    name: str
    roles: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Team:
    members: dict[str, Member] = field(default_factory=dict)

    def is_admin(self, username: str) -> bool:
        m = self.members.get(username.lower())
        return bool(m and ADMIN in m.roles)

    @property
    def admins(self) -> list[str]:
        return sorted(u for u, m in self.members.items() if ADMIN in m.roles)

    def name(self, username: str | None) -> str:
        if not username:
            return ""
        m = self.members.get(username.lower())
        return m.name if m else username

    def roles(self, username: str) -> list[str]:
        return [OPERATOR] + ([ADMIN] if self.is_admin(username) else [])

    def listed(self, username: str) -> bool:
        return username.lower() in self.members

    def choices(self, also: set[str] = frozenset()) -> list[tuple[str, str]]:
        """(username, display name) for assignment pickers: the team plus anyone else seen."""
        names = set(self.members) | {u.lower() for u in also if u}
        return sorted(((u, self.name(u)) for u in names), key=lambda x: x[1].lower())


def roles_errors(data: Any) -> list[str]:
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or 'roles.yaml'}: {e.message}"
              for e in jsonschema.Draft202012Validator(ROLES_SCHEMA).iter_errors(data)]
    if not errors:
        names = [u["username"].lower() for u in data["users"]]
        errors += [f"username {n!r} is listed more than once" for n in sorted({n for n in names if names.count(n) > 1})]
    return errors


def parse_team(data: Any) -> Team:
    errors = roles_errors(data)
    if errors:
        raise ValueError("; ".join(errors))
    return Team({u["username"].lower(): Member(u["username"].lower(), u.get("name") or u["username"],
                                               frozenset(u.get("roles") or ()))
                 for u in data["users"]})


def load_team(path: Path) -> Team:
    """The team from a roles file; an empty team if it doesn't exist."""
    if not path.is_file():
        return Team()
    return parse_team(yaml.safe_load(path.read_text(encoding="utf-8")))


class TeamSource:
    """Keeps the team up to date with the roles file, reloading when it changes."""

    def __init__(self, path: Path):
        self.path = path
        self._team = Team()
        self._stamp: tuple[float, int] | None = None
        self._lock = threading.Lock()
        self.refresh()

    @property
    def team(self) -> Team:
        with self._lock:
            return self._team

    def refresh(self) -> bool:
        """Reload if the file changed. Returns True if it was reloaded. A broken file keeps the last good team."""
        try:
            st = self.path.stat()
            stamp = (st.st_mtime, st.st_size)
        except FileNotFoundError:
            stamp = None
        if stamp == self._stamp:
            return False
        try:
            team = load_team(self.path)
        except (ValueError, OSError, yaml.YAMLError) as e:
            log.error("Ignoring invalid roles file %s: %s", self.path, e)
            return False
        with self._lock:
            self._team, self._stamp = team, stamp
        return True
