"""Per-user environment configuration loaded from `<WFT_HOME>/<env>.env`."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENTS = ("prod", "uat")
REPO_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    env: str
    share_root: Path
    port: int
    home: Path
    definitions_dir: Path

    @property
    def cache_path(self) -> Path:
        return self.home / "cache" / f"{self.env}.sqlite"


def wft_home() -> Path:
    """`%USERPROFILE%\\.wft` on Windows; overridable with WFT_HOME (used by tests)."""
    return Path(os.environ.get("WFT_HOME") or Path.home() / ".wft")


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep:
            raise ConfigError(f"Invalid line in env file: {raw!r}")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def load_config(env: str, home: Path | None = None) -> Config:
    if env not in ENVIRONMENTS:
        raise ConfigError(f"Unknown environment {env!r}; expected one of {', '.join(ENVIRONMENTS)}")
    home = home or wft_home()
    env_file = home / f"{env}.env"
    if not env_file.is_file():
        raise ConfigError(f"Env file not found: {env_file}. Copy .env.example there and fill it in.")
    values = parse_env_file(env_file.read_text(encoding="utf-8"))

    share_root = values.get("WFT_SHARE_ROOT")
    if not share_root:
        raise ConfigError(f"WFT_SHARE_ROOT is not set in {env_file}")
    try:
        port = int(values.get("WFT_PORT", "8765"))
    except ValueError as e:
        raise ConfigError(f"WFT_PORT must be a number in {env_file}") from e

    definitions_dir = Path(values.get("WFT_DEFINITIONS_DIR") or REPO_ROOT / "definitions")
    return Config(env=env, share_root=Path(share_root), port=port, home=home,
                  definitions_dir=definitions_dir)
