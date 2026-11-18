import copy

import pytest

from tests.defs import MANUAL, write_defs
from wft.config import REPO_ROOT
from wft.validation import validate_repo

INPUTS = {
    "type": "object",
    "properties": {
        "account": {"type": "string"},
        "limits": {"type": "object", "properties": {"max": {"type": "number"}}},
        "accounts": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
    },
    "required": ["account"],
}


def wf(**changes):
    d = copy.deepcopy(MANUAL)
    d.update(changes)
    return d


def errors(tmp_path, *workflows, files=None, share=None):
    report = validate_repo(write_defs(tmp_path / "defs", *workflows, files=files), share)
    return [i.message for i in report.errors]


def has(errs, text):
    return any(text in e for e in errs)


def test_repo_definitions_are_valid():
    report = validate_repo(REPO_ROOT / "definitions")
    assert report.ok, [str(i) for i in report.issues]
    assert report.checked


def test_simple_workflow_is_valid(tmp_path):
    assert errors(tmp_path, wf()) == []


@pytest.mark.parametrize("change, message", [
    ({"version": 0}, "minimum of 1"),
    ({"id": "Bad-Id"}, "does not match"),
    ({"steps": []}, "should be non-empty"),
    ({"surprise": 1}, "Additional properties"),
    ({"min_app_version": "1.0"}, "does not match"),
])
def test_schema_errors(tmp_path, change, message):
    d = wf(**change)
    if "id" in change:
        d["id"] = change["id"]
    assert has(errors(tmp_path, d), message)


def test_step_fields_are_checked_per_type(tmp_path):
    d = wf()
    d["steps"][0]["schema"] = "x.schema.json"  # only form steps take a schema
    assert has(errors(tmp_path, d), "'schema' was unexpected")
    d = wf()
    d["steps"][0] = {"id": "a", "type": "form"}
    assert has(errors(tmp_path, d), "form step: 'schema' is a required property")


def test_file_name_must_match_id(tmp_path):
    root = write_defs(tmp_path / "defs", wf())
    (root / "workflows" / "simple.yaml").rename(root / "workflows" / "other.yaml")
    assert has([i.message for i in validate_repo(root).errors], "file name must match")


def test_unknown_dependency_self_dependency_and_cycle(tmp_path):
    d = wf()
    d["steps"][0]["depends_on"] = ["nope"]
    assert has(errors(tmp_path, d), "depends on unknown step 'nope'")
    d = wf()
    d["steps"][0]["depends_on"] = ["a"]
    assert has(errors(tmp_path / "2", d), "depends on itself")
    d = wf()
    d["steps"][0]["depends_on"] = ["b"]
    assert has(errors(tmp_path / "3", d), "dependency cycle: a -> b -> a")


def test_duplicate_step_ids(tmp_path):
    d = wf()
    d["steps"][1]["id"] = "a"
    d["steps"][1].pop("depends_on")
    assert has(errors(tmp_path, d), "step id 'a' is used more than once")


def test_unsupported_step_types_are_reported(tmp_path):
    d = wf()
    d["steps"].append({"id": "c", "type": "outcome", "depends_on": ["b"], "outcomes": {"ok": {}}})
    assert has(errors(tmp_path, d), "step type(s) outcome not supported")


def test_outcome_routes_must_exist(tmp_path):
    d = wf()
    d["steps"].append({"id": "c", "type": "outcome", "depends_on": ["b"],
                       "outcomes": {"ok": {"next": "ghost"}, "bad": {"choices": ["close_rejected"]}}})
    assert has(errors(tmp_path, d), "outcome 'ok' refers to unknown step 'ghost'")
    d["steps"][-1]["outcomes"]["bad"] = {"choices": ["shrug"]}
    assert has(errors(tmp_path / "2", d), "'shrug' is not one of")


def test_decision_routes_and_unreachable_steps(tmp_path):
    d = wf()
    d["steps"].append({"id": "c", "type": "decision", "depends_on": ["b"],
                       "branches": [{"when": "{{ inputs.account == 'X' }}", "next": "nowhere"}]})
    assert has(errors(tmp_path, d), "branch 1 refers to unknown step 'nowhere'")


def test_route_targets_count_for_reachability(tmp_path):
    from wft.validation import _unreachable
    steps = [
        {"id": "start", "type": "manual"},
        {"id": "pick", "type": "decision", "depends_on": ["start"], "branches": [{"when": "x", "next": "x"}]},
        {"id": "x", "type": "manual", "depends_on": ["orphan"]},
        {"id": "orphan", "type": "manual", "depends_on": ["x"]},
    ]
    assert _unreachable(steps) == []  # x is a route target; orphan follows x
    assert _unreachable(steps[2:]) == ["x", "orphan"]


def test_referenced_files_must_exist_and_be_valid(tmp_path):
    d = wf(inputs_schema="tt/in.schema.json")
    d["steps"][0]["checklist"] = "tt/a.md"
    errs = errors(tmp_path, d)
    assert has(errs, "referenced file not found: schemas/tt/in.schema.json")
    assert has(errs, "referenced file not found: checklists/tt/a.md")
    errs = errors(tmp_path / "2", d, files={"schemas/tt/in.schema.json": {"type": "nonsense"},
                                            "checklists/tt/a.md": "# heading only\n"})
    assert has(errs, "schemas/tt/in.schema.json is not valid")
    assert has(errs, "has no '- item' lines")


def test_markdown_checklist_is_loaded(tmp_path):
    from wft.definitions import DefinitionRegistry
    d = wf()
    d["steps"][0]["checklist"] = "tt/a.md"
    root = write_defs(tmp_path / "defs", d, files={"checklists/tt/a.md": "# Do it\n\n- [ ] First\n- Second\n* Third\n"})
    assert validate_repo(root).ok
    assert DefinitionRegistry.from_repo(root).get("simple@1").step("a").checklist == ("First", "Second", "Third")


def test_consumes_must_exist_in_inputs_schema(tmp_path):
    d = wf(inputs_schema="in.schema.json")
    d["steps"][0]["consumes"] = ["account", "limits.*", "limits.max", "accounts.id", "limits.min", "nope.*"]
    errs = errors(tmp_path, d, files={"schemas/in.schema.json": INPUTS, "samples/simple.yaml": {"account": "A"}})
    assert [e for e in errs if "consumes" in e] == [
        "step 'a' consumes 'limits.min', which is not in in.schema.json",
        "step 'a' consumes 'nope.*', which is not in in.schema.json",
    ]


def test_consumes_without_inputs_schema(tmp_path):
    d = wf()
    d["steps"][0]["consumes"] = ["account"]
    assert has(errors(tmp_path, d), "no inputs_schema")


def with_capture(**changes):
    d = wf(inputs_schema="in.schema.json", **changes)
    d["steps"].insert(0, {"id": "capture", "type": "form", "schema": "in.schema.json"})
    d["steps"][1]["depends_on"] = ["capture"]
    return d


def test_form_steps_need_samples_that_match_their_schema(tmp_path):
    d = with_capture()
    files = {"schemas/in.schema.json": INPUTS}
    assert has(errors(tmp_path, d, files=files), "form steps need sample data at samples/simple.yaml")
    errs = errors(tmp_path / "2", d, files={**files, "samples/simple.yaml": {"capture": {"limits": {}}, "a": {}}})
    assert has(errs, "capture: 'account' is a required property")
    assert has(errs, "'a' is not a form step")
    assert errors(tmp_path / "3", d, files={**files, "samples/simple.yaml": {"capture": {"account": "A"}}}) == []


def test_inputs_schema_needs_an_initial_form(tmp_path):
    assert has(errors(tmp_path, wf(inputs_schema="in.schema.json"), files={"schemas/in.schema.json": INPUTS}),
               "no initial form step")


def test_form_schema_must_be_an_object(tmp_path):
    d = with_capture()
    files = {"schemas/in.schema.json": {"type": "string"}, "samples/simple.yaml": {"capture": "x"}}
    assert has(errors(tmp_path, d, files=files), 'a form schema must have "type": "object"')


def test_expressions_are_checked(tmp_path):
    d = wf(inputs_schema="in.schema.json")
    d["steps"][1]["on_cancel"] = {"flag": "Undo {{ steps.ghost.reference }}"}
    d["steps"][1]["compensation"] = {"checklist": "{{ inputs.nope }} {{ open('x') }}"}
    d["steps"][0]["on_hold"] = {"flag": "{{ item.id }}"}
    files = {"schemas/in.schema.json": INPUTS, "samples/simple.yaml": {"account": "A"}}
    errs = errors(tmp_path, d, files=files)
    assert has(errs, "refers to unknown step 'ghost'")
    assert has(errs, "Unknown name 'open'")
    assert has(errs, "`item` is only available")
    d["steps"][1]["compensation"] = {"checklist": "{{ inputs.nope }}"}
    assert has(errors(tmp_path / "2", d, files=files), "inputs.nope is not in in.schema.json")


def test_subworkflow_must_name_a_known_workflow(tmp_path):
    d = wf()
    d["steps"].append({"id": "c", "type": "subworkflow", "workflow": "ghost", "depends_on": ["b"]})
    assert has(errors(tmp_path, d), "sub-workflow 'ghost' is not a known workflow")


def test_min_app_version_newer_than_checkout_is_a_warning(tmp_path):
    report = validate_repo(write_defs(tmp_path / "defs", wf(min_app_version="99.0.0")))
    assert report.ok
    assert any("requires app 99.0.0" in i.message for i in report.issues)


def test_dry_run_detects_stuck_paths(tmp_path):
    from wft.definitions import parse_workflow
    from wft.validation import dry_run
    good = parse_workflow(wf())
    assert dry_run(good) == []
    # A step waiting on a dependency that is never completed (bypasses static checks).
    stuck = parse_workflow(wf(steps=[
        {"id": "a", "type": "manual", "depends_on": ["b"]},
        {"id": "b", "type": "manual", "depends_on": ["a"]},
    ]))
    [problem] = dry_run(stuck)
    assert "gets stuck with steps a, b unfinished" in problem


def test_dry_run_explores_each_order_of_parallel_steps():
    from wft.definitions import parse_workflow
    from wft.validation import dry_run
    parallel = parse_workflow(wf(steps=[
        {"id": "a", "type": "manual"}, {"id": "b", "type": "manual"},
        {"id": "c", "type": "manual", "depends_on": ["a", "b"]},
    ]))
    assert dry_run(parallel) == []
