from tests.defs import CAPTURE_SCHEMA
from wft.engine import validate_form
from wft.web.forms import build_fields, parse_form


def by_name(fields):
    out = {}
    for f in fields:
        out[f.name] = f
        out.update(by_name(f.children))
        for row in f.rows:
            out.update(by_name(row))
    return out


def test_field_kinds_follow_the_schema():
    fields = by_name(build_fields(CAPTURE_SCHEMA, {}, prefix="capture."))
    kinds = {name: f.kind for name, f in fields.items()}
    assert kinds == {
        "capture.account": "text", "capture.limits": "group", "capture.limits.max": "number",
        "capture.limits.currency": "select", "capture.markets": "choices", "capture.users": "rows",
        "capture.users.0.id": "text", "capture.users.0.access": "select", "capture.aliases": "lines",
        "capture.effective": "date", "capture.justification": "textarea", "capture.urgent": "checkbox",
    }
    assert fields["capture.account"].required and fields["capture.account"].label == "Account"
    assert fields["capture.limits.currency"].options == ["USD", "EUR"]


def test_existing_rows_plus_one_blank_row():
    fields = by_name(build_fields(CAPTURE_SCHEMA, {"users": [{"id": "a"}, {"id": "b"}]}))
    assert len(fields["users"].rows) == 3
    assert fields["users.1.id"].value == "b" and fields["users.2.id"].value is None


def test_errors_attach_to_fields():
    fields = by_name(build_fields(CAPTURE_SCHEMA, {}, {"limits.max": "Required", "users.0.id": "Required"}))
    assert fields["limits.max"].error == "Required" and fields["users.0.id"].error == "Required"


def test_parse_round_trip():
    form = {
        "capture.account": [" ACC1 "], "capture.limits.max": ["2500"], "capture.limits.currency": ["EUR"],
        "capture.markets": ["CME", "EUREX"], "capture.users.0.id": ["jdoe"], "capture.users.0.access": ["trade"],
        "capture.users.1.id": [""], "capture.users.1.access": [""], "capture.aliases": ["one\n\n two \n"],
        "capture.effective": ["2026-12-01"], "capture.justification": [""],
        "capture.urgent": ["on"], "capture.urgent.__present": ["1"],
    }
    data = parse_form(CAPTURE_SCHEMA, form, "capture.")
    assert data == {
        "account": "ACC1", "limits": {"max": 2500, "currency": "EUR"}, "markets": ["CME", "EUREX"],
        "users": [{"id": "jdoe", "access": "trade"}], "aliases": ["one", "two"], "effective": "2026-12-01",
        "urgent": True,
    }
    assert validate_form(CAPTURE_SCHEMA, data) == {}


def test_blank_and_bad_input_gives_clear_errors():
    data = parse_form(CAPTURE_SCHEMA, {"limits.max": ["lots"], "limits.currency": ["GBP"], "users.0.access": ["view"],
                                       "urgent.__present": ["1"]})
    assert data["urgent"] is False and data["limits"]["max"] == "lots"
    assert validate_form(CAPTURE_SCHEMA, data) == {
        "account": "Required",
        "limits.max": "Enter a number",
        "limits.currency": "Choose one of: USD, EUR",
        "users.0.id": "Required",
    }
    assert validate_form(CAPTURE_SCHEMA, parse_form(CAPTURE_SCHEMA, {"limits.max": ["-1"], "account": ["A"]})) == {
        "limits.max": "Must be at least 0"}


def test_data_objects_are_shown_and_carried_through():
    schema = {"type": "object", "properties": {"current": {"type": "object"}, "a": {"type": "string"}}}
    [current, _] = build_fields(schema, {"current": {"max": 1000}})
    assert current.kind == "data" and current.value == {"max": 1000}
    form = {"current.__json": ['{"max": 1000}'], "a": ["x"]}
    assert parse_form(schema, form) == {"current": {"max": 1000}, "a": "x"}
    assert parse_form(schema, {"current.__json": ["not json"]}) == {}
