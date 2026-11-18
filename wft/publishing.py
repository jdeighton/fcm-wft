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


def publish(definitions_dir: Path, share_root: Path, publisher: str, *, commit: str | None = None,
            dirty: bool = False, app_version: str = __version__) -> PublishResult:
    report = validate_repo(definitions_dir, share_root, app_version)
    if not report.ok:
        raise PublishError(f"Validation failed with {len(report.errors)} error(s); nothing was published", report)

    result = PublishResult(report=report)
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
