"""Helpers to build a throwaway definitions folder for validation and publishing tests."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

MANUAL = {
    "id": "simple", "version": 1, "title": "Simple",
    "steps": [
        {"id": "a", "type": "manual", "checklist": ["one"]},
        {"id": "b", "type": "manual", "depends_on": ["a"], "checklist": ["two"]},
    ],
}


def write_defs(root: Path, *workflows: dict, files: dict[str, object] | None = None) -> Path:
    (root / "workflows").mkdir(parents=True, exist_ok=True)
    for wf in workflows:
        (root / "workflows" / f"{wf['id']}.yaml").write_text(yaml.safe_dump(wf, sort_keys=False))
    for rel, content in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            content = json.dumps(content) if rel.endswith(".json") else yaml.safe_dump(content)
        path.write_text(content)
    return root
