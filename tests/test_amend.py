"""Amendments: changed fields, impact flags and routing back."""
import pytest

from tests.conftest import DEMO, make_services
from wft import engine, timings
from wft.engine import CommandError, FormInvalid, consumes_match, field_changes

BASE = {"account": "ACC1", "limits": {"max": 10, "currency": "USD"}, "comment": "first"}


def inputs(**changes):
    import copy
    d = copy.deepcopy(BASE)
    for k, v in changes.items():
        if k == "max":
            d["limits"]["max"] = v
        else:
            d[k] = v
    return {"capture": d}


@pytest.fixture
def svc(share, tmp_path, amend_registry):
    return make_services(share, tmp_path / "a.sqlite", amend_registry)


def submitted(svc):
    rid = svc.create_request("amendable@1", "x")
    svc.submit(rid, inputs())
    return rid


def done(svc, rid, step):
    svc.set_checklist_item(rid, step, 0, True)
    svc.complete_step(rid, step)


def test_field_changes_and_consumes_matching():
    assert field_changes({"a": 1, "l": {"max": 1, "cur": "USD"}, "x": [1]},
                         {"a": 1, "l": {"max": 2, "cur": "USD"}, "x": [1, 2], "new": "y"}) == [
        ("l.max", 1, 2), ("new", None, "y"), ("x", [1], [1, 2])]
    assert field_changes({"l": {"max": 1}}, {}) == [("l", {"max": 1}, None)]  # a removed group is one change
    assert consumes_match("limits.*", "limits.max") and consumes_match("limits.*", "limits")
    assert consumes_match("account", "account") and consumes_match("account", "account.region")
    assert consumes_match("users.*.id", "users")
    assert not consumes_match("limits.max", "limits.currency") and not consumes_match("account", "accounts")


def test_amend_before_work_starts_just_updates_inputs(svc):
    rid = submitted(svc)
    svc.amend(rid, inputs(max=20))
    state = svc.get(rid)[0]
    assert state.inputs["limits"]["max"] == 20 and state.step("capture").data["limits"]["max"] == 20
    assert state.flags == () and state.current_step == "csv"


def test_amend_flags_affected_steps_and_routes_back(svc):
    rid = submitted(svc)
    done(svc, rid, "csv")
    done(svc, rid, "raise")
    svc.start_step(rid, "apply")  # started, but only uses `account`
    impact = svc.amend_preview(rid, inputs(max=2500))
    assert impact.changes == [("limits.max", 10, 2500)]
    assert [(s, hits) for s, _, hits in impact.flagged] == [("csv", ["limits.max"]), ("raise", ["limits.max"])]
    assert impact.route_to == ["csv"] and impact.reset == ["csv", "raise", "apply"]
    assert svc.get(rid)[0].flags == ()  # preview records nothing

    svc.amend(rid, inputs(max=2500))
    state = svc.get(rid)[0]
    flags = {f.step: f for f in state.flags}
    assert flags["csv"].action == "Needs re-check: limits.max changed" and flags["csv"].trigger == "amend"
    assert flags["raise"].action == "KRM22 request no longer matches: withdraw or update it"
    assert "apply" not in flags
    assert [(s.id, s.status) for s in state.steps] == [
        ("capture", "done"), ("csv", "ready"), ("raise", "pending"), ("apply", "pending")]
    assert state.step("raise").ticked == frozenset() and state.step("raise").completed_by is None
    assert timings.step_timing(state.step("raise")).finished_at is None  # reopened
    [event] = [e for e in svc.rm.history(rid) if e.type == engine.REQUEST_AMENDED]
    assert event.payload["changes"] == [{"field": "limits.max", "before": 10, "after": 2500}]


def test_amending_an_unused_field_raises_nothing(svc):
    rid = submitted(svc)
    done(svc, rid, "csv")
    done(svc, rid, "raise")
    svc.amend(rid, inputs(comment="clarified"))
    state = svc.get(rid)[0]
    assert state.flags == () and state.step("raise").status == "done" and state.current_step == "apply"


def test_amend_while_on_hold_then_redo_and_complete(svc):
    rid = submitted(svc)
    done(svc, rid, "csv")
    done(svc, rid, "raise")
    svc.hold(rid, "client checking numbers")
    svc.amend(rid, inputs(max=99))
    state = svc.get(rid)[0]
    assert state.status == engine.ON_HOLD and state.step("csv").status == "ready"
    svc.resume(rid)
    for step in ("csv", "raise", "apply"):
        done(svc, rid, step)
    state = svc.get(rid)[0]
    assert state.awaiting_flags  # the amend flags still need resolving
    for f in state.flags:
        svc.resolve_flag(rid, f.id, "actioned", "Re-done with the new limit")
    assert svc.get(rid)[0].status == engine.COMPLETED


def test_amend_rules(svc, share, tmp_path, registry):
    rid = svc.create_request("amendable@1", "draft")
    with pytest.raises(CommandError, match="Only Active or on-hold"):
        svc.amend(rid, inputs())
    svc.submit(rid, inputs())
    with pytest.raises(CommandError, match="Nothing has changed"):
        svc.amend(rid, inputs())
    with pytest.raises(FormInvalid) as exc:
        svc.amend(rid, {"capture": {"account": "ACC1"}})
    assert exc.value.errors == {"limits": "Required"}
    demo = make_services(share, tmp_path / "d.sqlite", registry, "bob")
    other = demo.create_request(DEMO, "no inputs")
    demo.submit(other, {})
    with pytest.raises(CommandError, match="no input form"):
        demo.amend(other, {})


def test_route_to_must_point_upstream(tmp_path):
    import copy

    from tests.defs import AMENDABLE, AMENDABLE_FILES, write_defs
    from wft.validation import validate_repo
    d = copy.deepcopy(AMENDABLE)
    d["steps"][1]["on_amend"] = {"route_to": "apply"}  # csv routing forwards
    report = validate_repo(write_defs(tmp_path / "d", d, files=AMENDABLE_FILES))
    assert any("on_amend.route_to 'apply' must be a step it depends on" in i.message for i in report.errors)
    assert validate_repo(write_defs(tmp_path / "ok", AMENDABLE, files=AMENDABLE_FILES)).ok
