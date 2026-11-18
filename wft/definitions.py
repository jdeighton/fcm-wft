"""Workflow definitions: dataclasses, loading, and the registry the app runs from.

Two layouts are loaded with the same code:

- the repo's `definitions/` folder (authoring):
      workflows/<id>.yaml, schemas/..., checklists/..., mappings/...
- the share (published, immutable, one folder per version):
      definitions/workflows/<id>/<version>/workflow.yaml (+ the files it references, + manifest.json)

References inside a definition are relative paths: schema refs under `schemas/`,
checklist refs under `checklists/`, mapping refs under `mappings/`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

log = logging.getLogger(__name__)

PUBLISHED_DIR = Path("definitions") / "workflows"
WORKFLOW_FILE = "workflow.yaml"
MANIFEST_FILE = "manifest.json"
_CHECKLIST_ITEM = re.compile(r"^\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+?)\s*$")


class DefinitionError(Exception):
    pass


@dataclass(frozen=True)
class StepDef:
    id: str
    type: str
    title: str
    checklist: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    form_schema: dict | None = field(default=None, compare=False, repr=False)  # loaded JSON Schema of a form step
    raw: dict = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class WorkflowDef:
    id: str
    version: int
    title: str
    steps: tuple[StepDef, ...] = ()
    min_app_version: str | None = None
    raw: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def step(self, step_id: str) -> StepDef:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def runnable_by(self, app_version: str) -> bool:
        return self.min_app_version is None or parse_version(app_version) >= parse_version(self.min_app_version)


def parse_version(text: str) -> tuple[int, ...]:
    return tuple(int(p) for p in text.split("."))


def parse_checklist_md(text: str) -> list[str]:
    """Bullet items (`- item`, `* item`, `- [ ] item`) from a Markdown checklist."""
    return [m.group(1) for line in text.splitlines() if (m := _CHECKLIST_ITEM.match(line))]


def referenced_files(data: dict) -> list[str]:
    """Relative paths (from the definitions root) of every file a definition references."""
    refs = []
    if isinstance(data.get("inputs_schema"), str):
        refs.append(f"schemas/{data['inputs_schema']}")
    for s in data.get("steps") or ():
        if not isinstance(s, dict):
            continue
        if isinstance(s.get("schema"), str):
            refs.append(f"schemas/{s['schema']}")
        if isinstance(s.get("checklist"), str):
            refs.append(f"checklists/{s['checklist']}")
        for a in s.get("artefacts") or ():
            if isinstance(a, dict) and isinstance(a.get("mapping"), str):
                refs.append(f"mappings/{a['mapping']}")
    return sorted(set(refs))


def parse_workflow(data: dict, base: Path | None = None) -> WorkflowDef:
    """Build a WorkflowDef from parsed YAML. `base` is the definitions root, for Markdown checklists."""
    try:
        steps = []
        for s in data["steps"]:
            checklist = s.get("checklist", ())
            if isinstance(checklist, str):
                if base is None:
                    raise DefinitionError(f"Step {s['id']!r}: checklist file {checklist!r} needs a base folder")
                checklist = parse_checklist_md((base / "checklists" / checklist).read_text(encoding="utf-8"))
            form_schema = None
            if isinstance(s.get("schema"), str) and base is not None:
                form_schema = json.loads((base / "schemas" / s["schema"]).read_text(encoding="utf-8"))
            steps.append(StepDef(
                id=s["id"], type=s["type"], title=s.get("title", s["id"]),
                checklist=tuple(checklist), depends_on=tuple(s.get("depends_on", ())),
                form_schema=form_schema, raw=s,
            ))
        return WorkflowDef(id=data["id"], version=int(data["version"]), title=data["title"],
                           steps=tuple(steps), min_app_version=data.get("min_app_version"), raw=data)
    except (KeyError, TypeError, ValueError, OSError) as e:
        raise DefinitionError(f"Invalid workflow definition: {e!r}") from e


def read_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_repo(definitions_dir: Path) -> list[WorkflowDef]:
    """Current workflows from the repo authoring layout."""
    return [parse_workflow(read_yaml(f), definitions_dir)
            for f in sorted((definitions_dir / "workflows").glob("*.yaml"))]


def published_version_dirs(share_root: Path) -> list[Path]:
    """Complete published version folders (staging folders and half-copied ones are ignored)."""
    root = share_root / PUBLISHED_DIR
    return sorted(d for d in root.glob("*/*")
                  if d.name.isdigit() and (d / WORKFLOW_FILE).is_file() and (d / MANIFEST_FILE).is_file())


def load_published(share_root: Path, skip: Iterable[str] = ()) -> list[WorkflowDef]:
    skip = set(skip)
    out = []
    for d in published_version_dirs(share_root):
        if f"{d.parent.name}@{d.name}" in skip:
            continue
        try:
            out.append(parse_workflow(read_yaml(d / WORKFLOW_FILE), d))
        except (DefinitionError, OSError, yaml.YAMLError) as e:
            log.error("Can't load published definition %s: %s", d, e)
    return out


class DefinitionRegistry:
    """All workflow versions the app knows about, keyed `<id>@<version>`.

    Versions are only ever added, never replaced: a request keeps running on the
    exact version it started on.
    """

    def __init__(self, workflows: Iterable[WorkflowDef] = (),
                 loader: Callable[[set[str]], list[WorkflowDef]] | None = None):
        self._by_key: dict[str, WorkflowDef] = {}
        self._loader = loader
        self._lock = threading.Lock()
        self._add(workflows)

    @classmethod
    def from_repo(cls, definitions_dir: Path) -> DefinitionRegistry:
        """Unpublished definitions straight from the repo (development and tests)."""
        reg = cls(loader=lambda known: load_repo(definitions_dir))
        reg.refresh()
        return reg

    @classmethod
    def from_share(cls, share_root: Path) -> DefinitionRegistry:
        reg = cls(loader=lambda known: load_published(share_root, skip=known))
        reg.refresh()
        return reg

    def refresh(self) -> list[str]:
        """Load any newly published versions. Returns the keys added."""
        if self._loader is None:
            return []
        with self._lock:
            known = set(self._by_key)
        return self._add(self._loader(known))

    def _add(self, workflows: Iterable[WorkflowDef]) -> list[str]:
        added = []
        with self._lock:
            for w in workflows:
                if w.key not in self._by_key:
                    self._by_key[w.key] = w
                    added.append(w.key)
        return added

    def get(self, key: str) -> WorkflowDef:
        with self._lock:
            return self._by_key[key]

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._by_key)

    def latest(self) -> list[WorkflowDef]:
        """The newest version of each workflow, for starting new requests."""
        newest: dict[str, WorkflowDef] = {}
        with self._lock:
            for w in self._by_key.values():
                if w.id not in newest or w.version > newest[w.id].version:
                    newest[w.id] = w
        return sorted(newest.values(), key=lambda w: w.title)
