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


AMEND_SCHEMA = {"type": "object", "required": ["account", "limits"], "properties": {
    "account": {"type": "string"},
    "limits": {"type": "object", "required": ["max"], "properties": {"max": {"type": "number"}, "currency": {"type": "string"}}},
    "comment": {"type": "string"},
}}
AMENDABLE = {
    "id": "amendable", "version": 1, "title": "Amendable limit change", "inputs_schema": "amend.schema.json",
    "steps": [
        {"id": "capture", "type": "form", "schema": "amend.schema.json"},
        {"id": "csv", "type": "manual", "title": "Generate CSV", "depends_on": ["capture"],
         "checklist": ["Generate"], "consumes": ["account", "limits.*"]},
        {"id": "raise", "type": "manual", "title": "Raise in KRM22", "depends_on": ["csv"], "checklist": ["Upload"],
         "consumes": ["account", "limits.*"],
         "on_amend": {"flag": "KRM22 request no longer matches: withdraw or update it", "route_to": "csv"}},
        {"id": "apply", "type": "manual", "title": "Apply", "depends_on": ["raise"], "checklist": ["Apply"],
         "consumes": ["account"]},
    ],
}
AMENDABLE_FILES = {"schemas/amend.schema.json": AMEND_SCHEMA,
                   "samples/amendable.yaml": {"capture": {"account": "ACC1", "limits": {"max": 10}}}}


TT_CAPTURE = {"type": "object", "required": ["account", "limits"], "properties": {
    "account": {"type": "string", "x-picklist": "tt.accounts"},
    "current": {"type": "object"},
    "limits": {"type": "object", "required": ["max"], "properties": {
        "max": {"type": "number"}, "currency": {"type": "string"}}},
}}
TT_FLOW = {
    "id": "tt_limits", "version": 1, "title": "TT limits", "inputs_schema": "tt_capture.schema.json",
    "steps": [
        {"id": "capture", "type": "form", "schema": "tt_capture.schema.json",
         "prefill": {"operation": "tt.account_limits", "key": "account", "target": "current"}},
        {"id": "apply", "type": "manual", "depends_on": ["capture"], "checklist": ["Apply in TT"]},
        {"id": "check", "type": "verify", "title": "Verify in TT", "depends_on": ["apply"],
         "adapter": "tt.account_limits", "key": "{{ inputs.account }}", "expected": "{{ inputs.limits }}"},
    ],
}
TT_FILES = {"schemas/tt_capture.schema.json": TT_CAPTURE,
            "samples/tt_limits.yaml": {"capture": {"account": "ACC1", "limits": {"max": 1000}}}}


CHILD_SCHEMA = {"type": "object", "required": ["account", "access"], "properties": {
    "account": {"type": "string"}, "access": {"type": "string", "enum": ["trade", "view"]}}}
CHILD = {
    "id": "demo_account", "version": 1, "title": "Demo: set up account", "reference_prefix": "ACT",
    "inputs_schema": "child.schema.json",
    "steps": [
        {"id": "capture", "type": "form", "schema": "child.schema.json"},
        {"id": "setup", "type": "manual", "title": "Set up account", "depends_on": ["capture"], "checklist": ["Done"]},
    ],
}
PARENT_SCHEMA = {"type": "object", "required": ["client"], "properties": {
    "client": {"type": "string"}, "urgent": {"type": "boolean"},
    "accounts": {"type": "array", "items": {"type": "object", "properties": CHILD_SCHEMA["properties"]}}}}
PARENT = {
    "id": "demo_client", "version": 1, "title": "Demo: onboard client", "reference_prefix": "CLI",
    "inputs_schema": "parent.schema.json",
    "steps": [
        {"id": "capture", "type": "form", "schema": "parent.schema.json"},
        {"id": "route", "type": "decision", "title": "Urgent?", "depends_on": ["capture"],
         "branches": [{"when": "{{ inputs.urgent == true }}", "next": "fast"}], "default": "normal"},
        {"id": "fast", "type": "manual", "title": "Fast-track checks", "depends_on": ["route"], "checklist": ["Phone risk"]},
        {"id": "normal", "type": "manual", "title": "Standard checks", "depends_on": ["route"], "checklist": ["Email risk"]},
        {"id": "signoff", "type": "approval", "title": "Head of ops sign-off", "depends_on": ["fast", "normal"]},
        {"id": "accounts", "type": "subworkflow", "title": "Set up accounts", "depends_on": ["signoff"],
         "workflow": "demo_account", "for_each": "{{ inputs.accounts }}",
         "map_inputs": {"account": "{{ item.account }}", "access": "{{ item.access }}"}},
        {"id": "welcome", "type": "manual", "title": "Send welcome pack", "depends_on": ["accounts"], "checklist": ["Sent"]},
    ],
}
FAMILY_FILES = {
    "schemas/child.schema.json": CHILD_SCHEMA, "schemas/parent.schema.json": PARENT_SCHEMA,
    "samples/demo_account.yaml": {"capture": {"account": "A1", "access": "trade"}},
    "samples/demo_client.yaml": {"capture": {"client": "Acme", "accounts": [{"account": "A1", "access": "trade"}]}},
}
