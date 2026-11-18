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


CAPTURE_SCHEMA = {
    "type": "object",
    "required": ["account", "limits"],
    "properties": {
        "account": {"type": "string", "title": "Account", "minLength": 1},
        "limits": {"type": "object", "required": ["max"], "properties": {
            "max": {"type": "number", "minimum": 0, "title": "Max position"},
            "currency": {"type": "string", "enum": ["USD", "EUR"]},
        }},
        "markets": {"type": "array", "items": {"type": "string", "enum": ["CME", "ICE", "EUREX"]}},
        "users": {"type": "array", "items": {"type": "object", "required": ["id"], "properties": {
            "id": {"type": "string"}, "access": {"type": "string", "enum": ["trade", "view"]}}}},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "effective": {"type": "string", "format": "date"},
        "justification": {"type": "string", "maxLength": 2000},
        "urgent": {"type": "boolean"},
    },
}
APPROVAL_SCHEMA = {"type": "object", "required": ["ticket"], "properties": {"ticket": {"type": "string"}}}

FORMS = {
    "id": "limits", "version": 1, "title": "Limit change", "reference_prefix": "LIM",
    "inputs_schema": "capture.schema.json",
    "steps": [
        {"id": "capture", "type": "form", "title": "Capture", "schema": "capture.schema.json"},
        {"id": "apply", "type": "manual", "depends_on": ["capture"], "checklist": ["Apply it"],
         "consumes": ["account", "limits.*"]},
        {"id": "record", "type": "form", "depends_on": ["apply"], "schema": "approval.schema.json"},
    ],
}
FORMS_FILES = {
    "schemas/capture.schema.json": CAPTURE_SCHEMA,
    "schemas/approval.schema.json": APPROVAL_SCHEMA,
    "samples/limits.yaml": {"capture": {"account": "ACC1", "limits": {"max": 10}}, "record": {"ticket": "K-1"}},
}


ACCOUNT_SCHEMA = {"type": "object", "required": ["account"], "properties": {"account": {"type": "string"}}}
LIFECYCLE = {
    "id": "krm", "version": 1, "title": "KRM22 change", "reference_prefix": "KRM",
    "steps": [
        {"id": "capture", "type": "form", "schema": "account.schema.json"},
        {"id": "raise", "type": "manual", "title": "Raise in KRM22", "depends_on": ["capture"],
         "checklist": ["Upload CSV"],
         "on_hold": {"flag": "KRM22 may still approve {{ inputs.account }}"},
         "on_cancel": {"flag": "Withdraw the {{ inputs.account }} request in KRM22"},
         "compensation": {"checklist": "Withdraw KRM22 request for {{ inputs.account }}"}},
        {"id": "apply", "type": "manual", "title": "Apply", "depends_on": ["raise"], "checklist": ["Apply"]},
    ],
}
LIFECYCLE_FILES = {"schemas/account.schema.json": ACCOUNT_SCHEMA,
                   "samples/krm.yaml": {"capture": {"account": "ACC1"}}}
