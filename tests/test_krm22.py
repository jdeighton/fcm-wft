"""KRM22 CSV generation, external_request and outcome steps (#12)."""
import copy

import pytest
import yaml

from tests.conftest import make_services
from tests.defs import KRM_FLOW, krm22_mapping, krm_files, write_defs
from wft import engine
from wft.artefacts import ArtefactInvalid, generate_csv, mapping_errors
from wft.config import REPO_ROOT
from wft.engine import CommandError

ZN = {"exchange": "CME", "family": "CBOT", "type": "Future", "product": "ZN", "action": "add",
      "max_order_qty": 50, "max_position": 200}
PRODUCTS = [ZN,
            {"exchange": "CME", "family": "NYMEX", "type": "Option", "product": "CL, LO", "action": "change",
             "max_order_qty": 10, "max_position": 40.0},
            {"exchange": "Eurex", "family": "ALL", "type": "Option", "product": "OGBL", "action": "remove"}]
MAPPING = yaml.safe_load(krm22_mapping())


def ctx(products=PRODUCTS, account="ACC1", reference="LIM-2026-0007"):
    return {"inputs": {"account": account, "products": copy.deepcopy(products)},
            "request": {"reference": reference, "title": "t"}, "steps": {}}


# --- the generator ------------------------------------------------------------

def test_matches_the_expected_file_byte_for_byte():
    art = generate_csv(MAPPING, ctx())
    assert art.filename == "LIM-2026-0007_KRM22.csv"
    assert art.content == (REPO_ROOT / "tests/golden/krm22_tt_limits.csv").read_bytes()
    assert not art.content.startswith(b"\xef\xbb\xbf") and b"\r\n" in art.content


def test_header_lines_are_exactly_as_agreed():
    lines = generate_csv(MAPPING, ctx([ZN])).content.decode().split("\r\n")
    assert lines[0] == "Header,IsvInstance,IsvTeType,IsvTeName"
    assert lines[1].split(",")[:4] == ["Header", "IsvInstance", "IsvTeType", "IsvTeName"]
    assert len(lines[1].split(",")) == 22 and ", " not in lines[1]


@pytest.mark.parametrize("change, problem", [
    ({"type": "Futures"}, "Row 1 (CME/ZN): Type 'Futures' must be one of Future, Spread, Option"),
    ({"max_order_qty": 0}, "Max order quantity '0' must be a whole number greater than 0"),
    ({"max_position": 2.5}, "Max position product (net) '2.5' must be a whole number"),
    ({"max_position": None}, "Row 1 (CME/ZN): Max position product (net) is required"),
    ({"family": ""}, "Family is required"),
    ({"action": "swap"}, "action 'swap' must be one of add, change, remove"),
])
def test_field_level_problems(change, problem):
    with pytest.raises(ArtefactInvalid) as exc:
        generate_csv(MAPPING, ctx([{**ZN, **change}]))
    assert any(problem in p for p in exc.value.problems), exc.value.problems


def test_missing_account_products_or_reference():
    with pytest.raises(ArtefactInvalid) as exc:
        generate_csv(MAPPING, ctx([], account=""))
    assert any("no rows" in p for p in exc.value.problems)
    with pytest.raises(ArtefactInvalid) as exc:
        generate_csv(MAPPING, ctx([ZN], account=""))
    assert exc.value.problems == ["Row 1 (CME/ZN): IsvTeName is required"]
    with pytest.raises(ArtefactInvalid, match="file name"):
        generate_csv(MAPPING, ctx([ZN], reference=None))


def test_removed_rows_dont_need_quantities():
    art = generate_csv(MAPPING, ctx([{"exchange": "CME", "family": "CBOT", "type": "Future", "product": "ZN",
                                      "action": "remove"}]))
    assert art.content.decode().split("\r\n")[2].endswith(",Not Set,Not Set,Not Set,Not Set,Not Set,FALSE")


def test_mapping_checks():
    assert mapping_errors(MAPPING) == []
    bad = copy.deepcopy(MAPPING)
    bad["columns"].append({"name": "Exchange", "value": "{{ nope.x }}"})
    bad["columns"].append({"name": "Half", "add": "1"})
    errs = mapping_errors(bad)
    assert any("'Exchange' is listed more than once" in e for e in errs)
    assert any("Unknown name 'nope'" in e for e in errs)
    assert any("'Half' needs both `add` and `remove`" in e for e in errs)


# --- the workflow --------------------------------------------------------------

@pytest.fixture
def svc(share, tmp_path, krm22_registry):
    return make_services(share, tmp_path / "k.sqlite", krm22_registry)


def started(svc, products=(ZN,)):
    rid = svc.create_request("krm_limits@1", "ACC1 limits")
    svc.submit(rid, {"capture": {"account": "ACC1", "products": list(products)}})
    return rid


def raised(svc, products=(ZN,)):
    rid = started(svc, products)
    svc.generate_files(rid, "csv")
    svc.set_checklist_item(rid, "csv", 0, True)
    svc.complete_step(rid, "csv")
    svc.raise_external(rid, "raise", "KRM-778812", "https://krm.example.invalid/lur/778812")
    return rid


def test_definition_validates_and_dry_runs(tmp_path):
    from wft.validation import validate_repo
    report = validate_repo(write_defs(tmp_path / "d", KRM_FLOW, files=krm_files()))
    assert report.ok, [str(i) for i in report.issues]


def test_csv_is_generated_attached_and_required(svc):
    rid = started(svc)
    svc.set_checklist_item(rid, "csv", 0, True)
    with pytest.raises(CommandError, match="Generate the file"):
        svc.complete_step(rid, "csv")
    [name] = svc.generate_files(rid, "csv")
    state = svc.get(rid)[0]
    assert name == f"{state.reference}_KRM22.csv"
    [att] = engine.step_files(state, "csv")
    att, path = svc.attachment_file(rid, att.sha256)
    assert path.read_bytes().startswith(b"Header,IsvInstance,IsvTeType,IsvTeName\r\n") and att.step == "csv"
    svc.complete_step(rid, "csv")


def test_invalid_data_attaches_nothing(svc):
    no_qty = {k: v for k, v in ZN.items() if k != "max_order_qty"}  # the form allows it; the CSV doesn't
    rid = started(svc, [no_qty])
    with pytest.raises(ArtefactInvalid) as exc:
        svc.generate_files(rid, "csv")
    assert exc.value.problems == ["Row 1 (CME/ZN): Max order quantity is required"]
    assert svc.get(rid)[0].attachments == ()


def test_raising_records_the_reference_and_the_outcome_waits_on_krm22(svc):
    rid = raised(svc)
    state = svc.get(rid)[0]
    data = state.step("raise").data
    assert data["reference"] == "KRM-778812" and data["platform"] == "KRM22"
    assert [a["name"] for a in data["artefacts"]] == [f"{state.reference}_KRM22.csv"]
    result = state.step("result")
    assert result.status == engine.WAITING and result.wait_reason == "krm22" and "KRM-778812" in result.wait_note
    assert [w.step_id for w in svc.waiting_board()["krm22"]] == ["result"]
    other = started(svc)
    svc.generate_files(other, "csv")
    svc.set_checklist_item(other, "csv", 0, True)
    svc.complete_step(other, "csv")
    with pytest.raises(CommandError, match="Give the KRM22 reference"):
        svc.raise_external(other, "raise", "  ")
    with pytest.raises(CommandError, match="must start with http"):
        svc.raise_external(other, "raise", "KRM-9", "javascript:alert(1)")


def test_approved_goes_on(svc):
    rid = raised(svc)
    svc.record_outcome(rid, "result", "approved")
    state = svc.get(rid)[0]
    assert state.step("result").data["outcome"] == "approved" and state.step("confirm").status == engine.READY


def test_approved_with_changes_captures_applied_values_and_marks(svc):
    rid = raised(svc)
    with pytest.raises(CommandError, match="Add a note"):
        svc.record_outcome(rid, "result", "approved_with_changes")
    applied = [{**ZN, "max_order_qty": 25}]
    svc.record_outcome(rid, "result", "approved_with_changes", "Risk cut the order size", applied)
    data = svc.get(rid)[0].step("result").data
    assert data["applied_products"] == applied and data["marker"] == "differs_from_request"
    svc2_default = raised(svc)
    svc.record_outcome(svc2_default, "result", "approved_with_changes", "as asked")
    assert svc.get(svc2_default)[0].step("result").data["applied_products"] == [ZN]  # defaults to what was asked


def test_rejected_amend_and_resubmit_goes_back_to_the_csv(svc):
    rid = raised(svc)
    with pytest.raises(CommandError, match="Choose what happens next"):
        svc.record_outcome(rid, "result", "rejected", "too big")
    svc.record_outcome(rid, "result", "rejected", "Too big: ask for 100", choice="amend_and_resubmit")
    state = svc.get(rid)[0]
    assert state.status == engine.ACTIVE
    assert [(s.id, s.status) for s in state.steps][1:] == [
        ("csv", "ready"), ("raise", "pending"), ("result", "pending"), ("confirm", "pending")]
    svc.amend(rid, {"capture": {"account": "ACC1", "products": [{**ZN, "max_position": 100}]}})
    svc.set_checklist_item(rid, "csv", 0, True)
    with pytest.raises(CommandError, match="Generate the file"):  # the old CSV doesn't count any more
        svc.complete_step(rid, "csv")
    svc.generate_files(rid, "csv")
    svc.complete_step(rid, "csv")
    svc.raise_external(rid, "raise", "KRM-778900")
    svc.record_outcome(rid, "result", "approved")
    assert svc.get(rid)[0].step("confirm").status == engine.READY
    history = [e.payload.get("choice") for e in svc.rm.history(rid) if e.type == engine.OUTCOME_RECORDED]
    assert history == ["amend_and_resubmit", None]


def test_rejected_and_closed(svc):
    rid = raised(svc)
    svc.record_outcome(rid, "result", "rejected", "Not allowed", choice="close_rejected")
    state = svc.get(rid)[0]
    assert state.status == engine.REJECTED and state.rejected_at and state.step("confirm").status == engine.SKIPPED
    assert [r.request.request_id for r in svc.open_requests("rejected")] == [rid]
    follow = svc.create_follow_up(rid)
    assert svc.get(follow)[0].follow_up_of == rid


def test_withdrawn_only_while_cancelling_and_settles_the_flag(svc):
    rid = raised(svc)
    with pytest.raises(CommandError, match="only be recorded while the request is being cancelled"):
        svc.record_outcome(rid, "result", "withdrawn")
    svc.cancel(rid, "client withdrew")
    state = svc.get(rid)[0]
    assert state.status == engine.CANCELLING
    raise_flags = [f for f in state.flags if f.step == "raise" and f.blocking]
    assert [f.action for f in raise_flags] == ["Withdraw the request in KRM22"]
    for f in state.flags:
        if f.step != "raise":
            svc.resolve_flag(rid, f.id, "not_required", "nothing to undo")
    svc.record_outcome(rid, "result", "withdrawn", "Withdrawn in KRM22 by risk")
    state = svc.get(rid)[0]
    assert state.status == engine.CANCELLED
    assert state.flag(raise_flags[0].id).resolution == "actioned"


def test_close_rejected_needs_flags_resolved(svc):
    rid = raised(svc)
    svc.amend(rid, {"capture": {"account": "ACC1", "products": [{**ZN, "max_position": 300}]}})  # raises flags
    # The amend sent it back to the CSV step; raise and get rejected again with the flags still open.
    svc.generate_files(rid, "csv")
    svc.set_checklist_item(rid, "csv", 0, True)
    svc.complete_step(rid, "csv")
    svc.raise_external(rid, "raise", "KRM-2")
    with pytest.raises(CommandError, match="Resolve the open flags"):
        svc.record_outcome(rid, "result", "rejected", "no", choice="close_rejected")


def test_validation_of_outcome_routes(tmp_path):
    from wft.validation import validate_repo
    d = copy.deepcopy(KRM_FLOW)
    d["steps"][3]["outcomes"]["approved"]["next"] = "csv"  # going backwards
    d["steps"][3]["depends_on"] = ["csv"]                  # no external_request before it
    d["steps"][3]["outcomes"]["approved_with_changes"]["capture"] = {"name": "x", "from": "nope"}
    errs = [i.message for i in validate_repo(write_defs(tmp_path / "bad", d, files=krm_files())).errors]
    assert any("'approved' goes to 'csv', which must depend on the outcome step" in e for e in errs)
    assert any("offers amend_and_resubmit, so it must follow an external_request step" in e for e in errs)
    assert any("captures 'nope', which is not in" in e for e in errs)
    files = krm_files()
    files["mappings/krm22/tt_limit_upload.mapping.yaml"] = {"filename": "x.csv", "columns": [{"name": "A"}]}
    errs = [i.message for i in validate_repo(write_defs(tmp_path / "badmap", KRM_FLOW, files=files)).errors]
    assert any("column 'A' has no value" in e for e in errs)
