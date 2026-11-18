"""Server-side forms generated from JSON Schema.

`build_fields` turns a schema (plus current values and errors) into a tree of
`Field`s for the `_form.html` macros; `parse_form` turns the posted form back
into typed data for validation. Field names are dotted paths (`limits.max`,
`accounts.0.id`), matching the error keys from `engine.validate_form`.

Supported: string (text, textarea, date, select from enum), number, integer,
boolean, nested objects, arrays of enum values (checkboxes), arrays of scalars
(one per line) and arrays of objects (rows; a blank row is offered each time).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

TEXTAREA_OVER = 200


@dataclass
class Field:
    name: str
    label: str
    kind: str  # text textarea date number select checkbox choices lines group rows
    required: bool = False
    help: str | None = None
    value: Any = None
    options: list = field(default_factory=list)
    error: str | None = None
    step: str | None = None
    picklist: str | None = None         # name of the trading-system list offered as suggestions
    picklist_warning: str | None = None
    children: list[Field] = field(default_factory=list)
    rows: list[list[Field]] = field(default_factory=list)


def humanise(name: str) -> str:
    return re.sub(r"[_-]+", " ", name).strip().capitalize()


def kind_of(schema: dict) -> str:
    t = schema.get("type")
    if t == "object":
        # An object with no listed fields holds data filled in from elsewhere (e.g. `prefill`): shown, not edited.
        return "group" if schema.get("properties") else "data"
    if t == "array":
        items = schema.get("items") or {}
        if "enum" in items:
            return "choices"
        if items.get("type") == "object":
            return "rows"
        return "lines"
    if t == "boolean":
        return "checkbox"
    if t in ("number", "integer"):
        return "number"
    if "enum" in schema:
        return "select"
    if schema.get("format") == "date":
        return "date"
    if schema.get("x-widget") == "textarea" or (schema.get("maxLength") or 0) > TEXTAREA_OVER:
        return "textarea"
    return "text"


def build_fields(schema: dict, data: Any, errors: Mapping[str, str] | None = None,
                 prefix: str = "", picklists: Mapping[str, Any] | None = None) -> list[Field]:
    """`picklists` maps a picklist name (e.g. `tt.accounts`) to a `Picklist` of suggestions."""
    errors = errors or {}
    picklists = picklists or {}
    data = data if isinstance(data, dict) else {}
    required = set(schema.get("required") or ())
    out = []
    for key, sub in (schema.get("properties") or {}).items():
        name = f"{prefix}{key}"
        f = Field(name=name, label=sub.get("title") or humanise(key), kind=kind_of(sub),
                  required=key in required, help=sub.get("description"), value=data.get(key),
                  error=errors.get(name))
        if sub.get("x-picklist") and f.kind == "text":
            f.picklist = sub["x-picklist"]
            p = picklists.get(f.picklist)
            if p is not None:
                f.options = [(e.id, e.name) for e in p.items]
                if p.error:
                    f.picklist_warning = (f"Couldn't get the latest list ({p.error}). "
                                          + ("Showing the last saved list; " if p.items else "")
                                          + "you can type the value.")
        if f.kind == "group":
            f.children = build_fields(sub, data.get(key), errors, f"{name}.", picklists)
        elif f.kind == "rows":
            existing = data.get(key) if isinstance(data.get(key), list) else []
            item_schema = sub.get("items") or {}
            f.rows = [build_fields(item_schema, row, errors, f"{name}.{i}.", picklists)
                      for i, row in enumerate([*existing, {}])]
        elif f.kind == "choices":
            f.options = list(sub["items"]["enum"])
            f.value = data.get(key) or []
        elif f.kind == "select":
            f.options = list(sub["enum"])
        elif f.kind == "lines":
            f.value = "\n".join(str(v) for v in data.get(key) or [])
        elif f.kind == "number":
            f.step = "1" if sub.get("type") == "integer" else "any"
        out.append(f)
    return out


def parse_form(schema: dict, form: Mapping[str, list[str]], prefix: str = "") -> dict:
    """Posted values (name -> list of strings) to data shaped by the schema. Blank fields are left out."""
    out: dict = {}
    for key, sub in (schema.get("properties") or {}).items():
        name = f"{prefix}{key}"
        kind = kind_of(sub)
        if kind == "group":
            value = parse_form(sub, form, f"{name}.")
            if value:
                out[key] = value
        elif kind == "data":
            raw = _first(form, f"{name}.__json")
            if raw:
                try:
                    out[key] = json.loads(raw)
                except ValueError:
                    pass
        elif kind == "rows":
            indices = sorted({int(m.group(1)) for k in form
                              if (m := re.match(re.escape(name) + r"\.(\d+)\.", k))})
            rows = [parse_form(sub.get("items") or {}, form, f"{name}.{i}.") for i in indices]
            rows = [r for r in rows if r]  # untouched blank rows are dropped
            if rows:
                out[key] = rows
        elif kind == "choices":
            chosen = [v for v in form.get(name, []) if v]
            if chosen:
                out[key] = [_scalar(sub["items"], v) for v in chosen]
        elif kind == "lines":
            lines = [line.strip() for line in _first(form, name).splitlines() if line.strip()]
            if lines:
                out[key] = [_scalar(sub.get("items") or {}, v) for v in lines]
        elif kind == "checkbox":
            # A hidden "present" marker tells an unticked box apart from a field that wasn't on the form.
            if name in form or f"{name}.__present" in form:
                out[key] = "on" in form.get(name, [])
        else:
            raw = _first(form, name).strip()
            if raw != "":
                out[key] = _scalar(sub, raw)
    return out


def _first(form: Mapping[str, list[str]], name: str) -> str:
    values = form.get(name) or [""]
    return values[0]


def _scalar(schema: dict, raw: str) -> Any:
    """Coerce to the schema's type; leave unparseable input as text so validation explains the problem."""
    t = schema.get("type")
    try:
        if t == "integer":
            return int(raw)
        if t == "number":
            number = float(raw)
            return int(number) if number.is_integer() and "." not in raw else number
    except ValueError:
        return raw
    if t == "boolean":
        return raw.lower() in ("true", "on", "yes", "1")
    return raw


def picklist_names(schema: Any) -> set[str]:
    """Every `x-picklist` used in a form schema."""
    names: set[str] = set()
    if isinstance(schema, dict):
        if isinstance(schema.get("x-picklist"), str):
            names.add(schema["x-picklist"])
        for sub in (schema.get("properties") or {}).values():
            names |= picklist_names(sub)
        if isinstance(schema.get("items"), dict):
            names |= picklist_names(schema["items"])
    return names
