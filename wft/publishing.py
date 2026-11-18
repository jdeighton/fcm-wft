"""Publishing workflow definitions from the repo to the share.

Each workflow version is copied to its own folder, written under a staging name
and then renamed into place, so readers never see a half-published version.
Published versions are never changed afterwards.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from wft import __version__
from wft.definitions import MANIFEST_FILE, PUBLISHED_DIR
from wft.events import new_id, utc_now
from wft.team import REPO_ROLES_FILE, ROLES_FILE, load_team
from wft.validation import Report, bundle_files, fingerprint, published_manifest, validate_repo


class PublishError(Exception):
    def __init__(self, message: str, report: Report | None = None):
        super().__init__(message)
        self.report = report


@dataclass
class PublishResult:
    published: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    report: Report | None = None


def git_state(repo: Path) -> tuple[str | None, bool]:
    """(commit, has uncommitted changes under definitions/) or (None, False) outside git."""
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                                check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "definitions"], cwd=repo,
                               capture_output=True, text=True, check=True).stdout.strip() != ""
        return commit, dirty
    except (OSError, subprocess.CalledProcessError):
        return None, False


def publish(definitions_dir: Path, share_root: Path, publisher: str, *, username: str | None = None,
            commit: str | None = None, dirty: bool = False, app_version: str = __version__) -> PublishResult:
    """Validate and publish. `username` must be an admin in the environment's published roles (once it has any)."""
    username = (username or publisher.split("@")[0]).lower()
    current = load_team(share_root / ROLES_FILE)
    if current.admins and not current.is_admin(username):
        raise PublishError(f"Only admins can publish to this environment ({', '.join(current.admins)}); "
                           f"{username} is not one")
    new_roles = definitions_dir / REPO_ROLES_FILE
    if new_roles.is_file():
        try:
            proposed = load_team(new_roles)
        except ValueError:
            proposed = None  # validation below reports the problem
        if proposed and proposed.admins and not proposed.is_admin(username):
            raise PublishError(f"The roles file would make {', '.join(proposed.admins)} the admins but not you "
                               f"({username}); you'd be locked out of publishing")

    report = validate_repo(definitions_dir, share_root, app_version)
    if not report.ok:
        raise PublishError(f"Validation failed with {len(report.errors)} error(s); nothing was published", report)

    result = PublishResult(report=report)
    if new_roles.is_file():
        target = share_root / ROLES_FILE
        content = new_roles.read_bytes()
        if target.is_file() and target.read_bytes() == content:
            result.unchanged.append("roles")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(f".{target.name}.{new_id()}.tmp")
            tmp.write_bytes(content)
            os.replace(tmp, target)  # readers see the old or the new file, never half of one
            (target.with_name("roles.published.json")).write_text(json.dumps(
                {"published_at": utc_now(), "published_by": publisher, "git_commit": commit, "git_dirty": dirty},
                indent=2), encoding="utf-8")
            result.published.append("roles")
    for path in sorted((definitions_dir / "workflows").glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        key = f"{data['id']}@{data['version']}"
        files = bundle_files(path, definitions_dir, data)
        if published_manifest(share_root, data["id"], data["version"]) is not None:
            result.unchanged.append(key)  # validation has confirmed it's identical
            continue
        _install(share_root, data, files, {
            "id": data["id"], "version": data["version"], "files": fingerprint(files),
            "published_at": utc_now(), "published_by": publisher, "app_version": app_version,
            "git_commit": commit, "git_dirty": dirty,
        })
        result.published.append(key)
    return result


def _install(share_root: Path, data: dict, files: dict[str, bytes], manifest: dict) -> None:
    final = share_root / PUBLISHED_DIR / data["id"] / str(data["version"])
    staging = share_root / "definitions" / ".staging" / f"{data['id']}-{data['version']}-{new_id()}"
    try:
        for name, content in files.items():
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        (staging / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(staging, final)
        except OSError as e:
            existing = published_manifest(share_root, data["id"], data["version"])
            if existing and existing.get("files") == manifest["files"]:
                return  # someone published the identical version at the same moment
            raise PublishError(f"Could not publish {data['id']}@{data['version']}: {e}") from e
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
