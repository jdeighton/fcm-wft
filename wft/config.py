"""Per-user environment configuration loaded from `<WFT_HOME>/<env>.env`."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from wft.adapters.tt import TTSettings

ENVIRONMENTS = ("prod", "uat")
REPO_ROOT = Path(__file__).resolve().parent.parent
ENVIRONMENT_RULES = REPO_ROOT / "config" / "environments.yaml"
ENV_MARKER = Path("meta") / "environment"
# Every key an env file may contain; keep `.env.example` in step.
SUPPORTED_KEYS = ("WFT_SHARE_ROOT", "WFT_PORT", "WFT_DEFINITIONS_DIR",
                  "WFT_TT_BASE_URL", "WFT_TT_ENVIRONMENT", "WFT_TT_APP_KEY", "WFT_TT_APP_SECRET",
                  "WFT_TT_APP_NAME", "WFT_TT_COMPANY", "WFT_TT_CACHE_MINUTES", "WFT_NOTIFICATIONS")
SECRET_KEYS = ("WFT_TT_APP_KEY", "WFT_TT_APP_SECRET")  # never shown in messages or logs
FAKE_TT = "fake:"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    env: str
    share_root: Path
    port: int
    home: Path
    definitions_dir: Path | None  # development only: run unpublished definitions from a repo folder
    tt: TTSettings | None = None  # None: TT not configured (lookups fall back to typing)
    tt_fake: bool = False         # WFT_TT_BASE_URL=fake: uses sample data instead of TT
    picklist_ttl_minutes: float = 60.0
    notifications: str = ""  # windows | browser | off; empty: windows on Windows, else browser

    @property
    def last_online_path(self) -> Path:
        return self.home / f"last-online-{self.env}.json"

    @property
    def picklist_cache_path(self) -> Path:
        return self.home / "cache" / f"picklists-{self.env}.json"

    @property
    def cache_path(self) -> Path:
        # Keyed by share as well as env, so repointing an env file never shows another share's data.
        share = hashlib.sha256(str(self.share_root).lower().encode()).hexdigest()[:12]
        return self.home / "cache" / f"{self.env}-{share}.sqlite"


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


def check_environment(env: str, values: dict[str, str], rules: dict[str, dict[str, list[str]]]) -> None:
    """Refuse values that don't look like `env`, or that look like another environment."""
    problems = []
    for key, value in values.items():
        own = rules.get(env, {}).get(key)
        shown = "<hidden>" if key in SECRET_KEYS else value
        if own is not None and not any(re.search(p, value, re.IGNORECASE) for p in own):
            problems.append(f"{key}={shown} does not look like a {env} value")
        for other, other_rules in rules.items():
            if other != env and any(re.search(p, value, re.IGNORECASE) for p in other_rules.get(key, ())):
                problems.append(f"{key}={shown} looks like a {other} value")
    if problems:
        raise ConfigError(f"The {env} env file mixes environments:\n  " + "\n  ".join(problems))


def load_rules(path: Path = ENVIRONMENT_RULES) -> dict[str, dict[str, list[str]]]:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def check_share_marker(config: Config) -> None:
    """The share declares its own environment in `meta/environment`; it must match."""
    marker = config.share_root / ENV_MARKER
    try:
        declared = marker.read_text(encoding="utf-8").strip().lower()
    except FileNotFoundError:
        raise ConfigError(f"{marker} is missing. An admin must create it containing '{config.env}'.") from None
    if declared != config.env:
        raise ConfigError(f"Running as {config.env} but the share at {config.share_root} is marked '{declared}'.")


def load_config(env: str, home: Path | None = None,
                rules: dict[str, dict[str, list[str]]] | None = None) -> Config:
    if env not in ENVIRONMENTS:
        raise ConfigError(f"Unknown environment {env!r}; expected one of {', '.join(ENVIRONMENTS)}")
    home = home or wft_home()
    env_file = home / f"{env}.env"
    if not env_file.is_file():
        raise ConfigError(f"Env file not found: {env_file}. Copy .env.example there and fill it in.")
    values = parse_env_file(env_file.read_text(encoding="utf-8"))
    unknown = sorted(set(values) - set(SUPPORTED_KEYS))
    if unknown:
        raise ConfigError(f"Unknown key(s) in {env_file}: {', '.join(unknown)}")
    check_environment(env, values, load_rules() if rules is None else rules)

    share_root = values.get("WFT_SHARE_ROOT")
    if not share_root:
        raise ConfigError(f"WFT_SHARE_ROOT is not set in {env_file}")
    try:
        port = int(values.get("WFT_PORT", "8765"))
    except ValueError as e:
        raise ConfigError(f"WFT_PORT must be a number in {env_file}") from e

    definitions_dir = Path(values["WFT_DEFINITIONS_DIR"]) if values.get("WFT_DEFINITIONS_DIR") else None
    tt, tt_fake = _tt_settings(values, env_file)
    try:
        ttl = float(values.get("WFT_TT_CACHE_MINUTES", "60"))
    except ValueError as e:
        raise ConfigError(f"WFT_TT_CACHE_MINUTES must be a number in {env_file}") from e
    notifications = values.get("WFT_NOTIFICATIONS", "").strip().lower()
    if notifications not in ("", "windows", "browser", "off"):
        raise ConfigError(f"WFT_NOTIFICATIONS must be windows, browser or off in {env_file}")
    return Config(env=env, share_root=Path(share_root), port=port, home=home,
                  definitions_dir=definitions_dir, tt=tt, tt_fake=tt_fake, picklist_ttl_minutes=ttl,
                  notifications=notifications)


def _tt_settings(values: dict[str, str], env_file: Path) -> tuple[TTSettings | None, bool]:
    base = values.get("WFT_TT_BASE_URL", "").strip()
    if not base:
        return None, False
    if base == FAKE_TT:
        return None, True
    missing = [k for k in ("WFT_TT_ENVIRONMENT", "WFT_TT_APP_KEY", "WFT_TT_APP_SECRET") if not values.get(k)]
    if missing:
        raise ConfigError(f"WFT_TT_BASE_URL is set but {', '.join(missing)} missing in {env_file}")
    return TTSettings(base_url=base, environment=values["WFT_TT_ENVIRONMENT"], app_key=values["WFT_TT_APP_KEY"],
                      app_secret=values["WFT_TT_APP_SECRET"], app_name=values.get("WFT_TT_APP_NAME", "WFT"),
                      company=values.get("WFT_TT_COMPANY", "FIRM")), False
