"""Workflow definitions: dataclasses and a YAML loader.

Validation and publishing to the share arrive in a later ticket (#4); for now
definitions are read straight from the repo's `definitions/workflows` folder.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

STEP_TYPES = ("manual",)


class DefinitionError(Exception):
    pass


@dataclass(frozen=True)
class StepDef:
    id: str
    type: str
    title: str
    checklist: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkflowDef:
    id: str
    version: int
    title: str
    steps: tuple[StepDef, ...] = field(default_factory=tuple)

    @property
    def key(self) -> str:
        return f"{self.id}@{self.version}"

    def step(self, step_id: str) -> StepDef:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)


def parse_workflow(data: dict) -> WorkflowDef:
    try:
        steps = tuple(
            StepDef(
                id=s["id"],
                type=s["type"],
                title=s.get("title", s["id"]),
                checklist=tuple(s.get("checklist", ())),
                depends_on=tuple(s.get("depends_on", ())),
            )
            for s in data["steps"]
        )
        wf = WorkflowDef(id=data["id"], version=int(data["version"]), title=data["title"], steps=steps)
    except (KeyError, TypeError, ValueError) as e:
        raise DefinitionError(f"Invalid workflow definition: {e!r}") from e
    for s in wf.steps:
        if s.type not in STEP_TYPES:
            raise DefinitionError(f"{wf.key}: unsupported step type {s.type!r} in step {s.id!r}")
    return wf


class DefinitionRegistry:
    def __init__(self, workflows: list[WorkflowDef]):
        self._by_key = {w.key: w for w in workflows}

    @classmethod
    def load(cls, definitions_dir: Path) -> DefinitionRegistry:
        files = sorted((definitions_dir / "workflows").glob("*.yaml"))
        return cls([parse_workflow(yaml.safe_load(f.read_text(encoding="utf-8"))) for f in files])

    def get(self, key: str) -> WorkflowDef:
        return self._by_key[key]

    def latest(self) -> list[WorkflowDef]:
        """The newest version of each workflow, for starting new requests."""
        newest: dict[str, WorkflowDef] = {}
        for w in self._by_key.values():
            if w.id not in newest or w.version > newest[w.id].version:
                newest[w.id] = w
        return sorted(newest.values(), key=lambda w: w.title)
