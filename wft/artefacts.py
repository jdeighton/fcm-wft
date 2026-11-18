"""Files generated from a request for upload elsewhere, e.g. the KRM22 limit upload CSV. Pure functions.

A *mapping* (YAML, in the definitions' mappings folder) describes a CSV:

    filename: "{{ request.reference }}_KRM22.csv"
    header_lines:                    # written first, as-is
      - [Header, IsvInstance, IsvTeType, IsvTeName]
    rows_from: inputs.products       # one data row per item in this list (`row` in expressions)
    action_from: row.action          # optional: add | change | remove (change is written like add)
    columns:                         # in order; their names form the last header line
      - name: Header
        value: Data                  # fixed text
      - name: IsvTeName
        value: "{{ inputs.account }}"
        required: true
      - name: Type
        value: "{{ row.type }}"
        enum: [Future, Spread, Option]
      - name: Max order quantity
        value: "{{ row.max_order_qty }}"
        remove: Not Set              # used instead of `value` for rows being removed
        positive_integer: true
      - name: Trading Allowed
        add: "TRUE"                  # for rows being added or changed
        remove: Not Set

Files are UTF-8 without a BOM, CRLF line endings, fields quoted only when needed.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any

import jsonschema

from wft.expressions import ExpressionError, parse

GENERATORS = ("krm22.csv", "csv")  # both use a mapping; the name says what the file is for
ACTIONS = ("add", "change", "remove")

MAPPING_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["filename", "columns"],
    "properties": {
        "description": {"type": "string"},
        "filename": {"type": "string", "minLength": 1},
        "header_lines": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
        "rows_from": {"type": "string"},
        "action_from": {"type": "string"},
        "columns": {
            "type": "array", "minItems": 1,
            "items": {
                "type": "object", "additionalProperties": False, "required": ["name"],
                "properties": {
                    "name": {"type": "string", "minLength": 1},
                    "value": {"type": ["string", "number", "boolean"]},
                    "add": {"type": ["string", "number", "boolean"]},
                    "remove": {"type": ["string", "number", "boolean"]},
                    "required": {"type": "boolean"},
                    "enum": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "positive_integer": {"type": "boolean"},
                },
            },
        },
    },
}


class ArtefactInvalid(ValueError):
    """The request's data can't make a valid file. `problems` are user-facing, one per field."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Artefact:
    filename: str
    content: bytes


def mapping_errors(mapping: Any) -> list[str]:
    """Problems with a mapping file itself (checked by the validator)."""
    errors = [f"{'/'.join(str(p) for p in e.absolute_path) or 'mapping'}: {e.message}"
              for e in jsonschema.Draft202012Validator(MAPPING_SCHEMA).iter_errors(mapping)]
    if errors:
        return errors
    names = [c["name"] for c in mapping["columns"]]
    errors += [f"column {n!r} is listed more than once" for n in sorted({n for n in names if names.count(n) > 1})]
    for c in mapping["columns"]:
        if not any(k in c for k in ("value", "add", "remove")):
            errors.append(f"column {c['name']!r} has no value (give `value`, or `add` / `remove`)")
        if ("add" in c) != ("remove" in c) and "value" not in c:
            errors.append(f"column {c['name']!r} needs both `add` and `remove`, or a `value`")
        for key in ("value", "add", "remove"):
            if isinstance(c.get(key), str):
                try:
                    parse(c[key])
                except ExpressionError as e:
                    errors.append(f"column {c['name']!r} {key}: {e}")
    for key in ("filename", "rows_from", "action_from"):
        if mapping.get(key):
            try:
                parse(mapping[key] if key == "filename" else "{{ " + mapping[key] + " }}")
            except ExpressionError as e:
                errors.append(f"{key}: {e}")
    if any(("add" in c or "remove" in c) for c in mapping["columns"]) and not mapping.get("action_from"):
        errors.append("columns use add/remove values but the mapping has no `action_from`")
    return errors


def generate_csv(mapping: dict, context: dict) -> Artefact:
    """Build the file from a request's expression context (inputs, steps, request). Raises ArtefactInvalid."""
    problems: list[str] = []
    rows_src: list[Any] = [None]
    if mapping.get("rows_from"):
        value = _eval(mapping["rows_from"], context)
        rows_src = list(value) if isinstance(value, (list, tuple)) else []
        if not rows_src:
            problems.append(f"There are no rows to write ({mapping['rows_from']} is empty)")

    data_rows = []
    for i, row in enumerate(rows_src):
        ctx = {**context, "row": row if isinstance(row, dict) else {}}
        label = _row_label(row, i) if mapping.get("rows_from") else None
        where = f"Row {i + 1}{f' ({label})' if label else ''}: " if label is not None else ""
        action = "add"
        if mapping.get("action_from"):
            action = str(_eval(mapping["action_from"], ctx) or "add").strip().lower()
            if action not in ACTIONS:
                problems.append(f"{where}action {action!r} must be one of {', '.join(ACTIONS)}")
                action = "add"
        removing = action == "remove"
        out = []
        for col in mapping["columns"]:
            if removing and "remove" in col:
                raw = col["remove"]
            elif not removing and "add" in col:
                raw = col["add"]
            else:
                raw = col.get("value", "")
            value = _text(_render(raw, ctx))
            from_request = isinstance(raw, str) and "{{" in raw
            if from_request or not removing:
                problems += _check(col, value, where, removing and "remove" in col)
            out.append(value)
        data_rows.append(out)

    filename = _text(_render(mapping["filename"], context)).strip()
    if not filename or "{{" in mapping["filename"] and filename.startswith("_"):
        problems.append("The file name couldn't be worked out (does the request have a reference yet?)")
    if problems:
        raise ArtefactInvalid(problems)

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    for line in mapping.get("header_lines") or ():
        writer.writerow(line)
    writer.writerow([c["name"] for c in mapping["columns"]])
    writer.writerows(data_rows)
    return Artefact(_safe_name(filename), buffer.getvalue().encode("utf-8"))


def _check(col: dict, value: str, where: str, is_remove_value: bool) -> list[str]:
    if is_remove_value:
        return []
    name = col["name"]
    if col.get("required") and value.strip() == "":
        return [f"{where}{name} is required"]
    if value.strip() == "":
        return []
    if col.get("enum") and value not in col["enum"]:
        return [f"{where}{name} {value!r} must be one of {', '.join(col['enum'])}"]
    if col.get("positive_integer") and not re.fullmatch(r"[1-9]\d*", value.strip()):
        return [f"{where}{name} {value!r} must be a whole number greater than 0"]
    return []


def _eval(path: str, ctx: dict) -> Any:
    return parse("{{ " + path + " }}").render(ctx)


def _render(raw: Any, ctx: dict) -> Any:
    return parse(raw).render(ctx) if isinstance(raw, str) and "{{" in raw else raw


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _row_label(row: Any, index: int) -> str:
    if isinstance(row, dict):
        parts = [str(row[k]) for k in ("exchange", "product") if row.get(k)]
        if parts:
            return "/".join(parts)
    return f"#{index + 1}"


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)
