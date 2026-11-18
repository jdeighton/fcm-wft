"""Sub-workflows (fan-out and join), decisions, approvals, and hold/cancel passed on to child requests."""
import copy

import pytest

from tests.conftest import make_services
from tests.defs import CHILD, FAMILY_FILES, PARENT, write_defs
from wft import engine
from wft.engine import CommandError

ACCOUNTS = [{"account": "ACC1", "access": "trade"}, {"account": "ACC2", "access": "view"}]


@pytest.fixture
def pair(share, tmp_path, family_registry):
    alice = make_services(share, tmp_path / "a.sqlite", family_registry, "alice")
    bob = make_services(share, tmp_path / "b.sqlite", family_registry, "bob")
    for s, n in ((alice, "a"), (bob, "b")):
        s._acknowledged_path = tmp_path / f"ack-{n}.json"
    return alice, bob


def tick(svc, rid, step):
    svc.set_checklist_item(rid, step, 0, True)
    svc.complete_step(rid, step)


def client(svc, urgent=False, accounts=ACCOUNTS):
    rid = svc.create_request("demo_client@1", "Acme onboarding")
    svc.submit(rid, {"capture": {"client": "Acme", "urgent": urgent, "accounts": copy.deepcopy(accounts)}})
    return rid


def approved(alice, bob, **kw):
    rid = client(alice, **kw)
    tick(alice, rid, "normal" if not kw.get("urgent") else "fast")
    bob.sync()
    bob.approve(rid, "signoff", "Looks fine")
    alice.sync()
    return rid


def test_definitions_validate_and_dry_run(tmp_path):
    from wft.validation import validate_repo
    report = validate_repo(write_defs(tmp_path / "d", PARENT, CHILD, files=FAMILY_FILES))
    assert report.ok, [str(i) for i in report.issues]


def test_decision_takes_the_matching_branch_and_skips_the_other(pair):
    alice, _ = pair
    rid = client(alice, urgent=True)
    state = alice.get(rid)[0]
    assert state.step("route").status == engine.DONE and state.step("route").data["chosen"] == "fast"
    assert (state.step("fast").status, state.step("normal").status) == (engine.READY, engine.SKIPPED)
    rid2 = client(alice, urgent=False)
    state = alice.get(rid2)[0]
    assert state.step("route").data == {"chosen": "normal", "rule": "default"}
    assert state.step("fast").status == engine.SKIPPED
    tick(alice, rid2, "normal")
    assert alice.get(rid2)[0].step("signoff").status == engine.READY  # one option done, the other skipped


def test_unsettled_decision_is_chosen_by_hand(share, tmp_path):
    from wft.definitions import DefinitionRegistry
    d = copy.deepcopy(PARENT)
    del d["steps"][1]["default"]
    d["steps"][1]["branches"].append({"when": "{{ inputs.client == 'nobody' }}", "next": "normal"})
    reg = DefinitionRegistry.from_repo(write_defs(tmp_path / "d", d, CHILD, files=FAMILY_FILES))
    svc = make_services(share, tmp_path / "c.sqlite", reg)
    rid = client(svc, urgent=False)
    assert svc.get(rid)[0].step("route").status == engine.READY
    with pytest.raises(CommandError, match="Choose one"):
        svc.choose_branch(rid, "route", "welcome")
    svc.choose_branch(rid, "route", "normal")
    state = svc.get(rid)[0]
    assert state.step("route").data["rule"] == "chosen by hand" and state.step("fast").status == engine.SKIPPED


def test_approval_needs_someone_else_and_records_rejections(pair):
    alice, bob = pair
    rid = client(alice)
    tick(alice, rid, "normal")
    with pytest.raises(CommandError, match="Someone other than"):
        alice.approve(rid, "signoff")
    bob.sync()
    with pytest.raises(CommandError, match="Say why"):
        bob.reject_approval(rid, "signoff", " ")
    bob.reject_approval(rid, "signoff", "Missing the KYC pack")
    bob.approve(rid, "signoff", "KYC pack now attached")
    step = bob.get(rid)[0].step("signoff")
    assert step.status == engine.DONE and step.completed_by == "bob"
    assert step.data["rejections"][0]["notes"] == "Missing the KYC pack" and step.data["approval"] == "approved"


def test_subworkflow_fans_out_and_joins(pair):
    alice, bob = pair
    rid = approved(alice, bob)
    parent = alice.get(rid)[0]
    kids = parent.step("accounts").data["children"]
    assert parent.step("accounts").status == engine.IN_PROGRESS and len(kids) == 2
    children = alice.children(rid)
    assert [c.inputs for c in children] == ACCOUNTS
    assert all(c.status == engine.ACTIVE and c.parent["request"] == rid and c.reference.startswith("ACT-")
               for c in children)
    assert children[0].title.endswith("ACC1")
    # The approver (bob) started them, as part of approving.
    assert {e.actor for e in alice.rm.history(children[0].request_id)} == {"bob"}

    tick(alice, children[0].request_id, "setup")
    assert alice.get(rid)[0].step("accounts").status == engine.IN_PROGRESS
    bob.sync()
    tick(bob, children[1].request_id, "setup")  # the last child completes: bob's instance joins the parent
    alice.sync()
    parent = alice.get(rid)[0]
    assert parent.step("accounts").status == engine.DONE
    assert [k["status"] for k in parent.step("accounts").data["children"]] == ["Completed", "Completed"]
    assert parent.step("welcome").status == engine.READY
    assert alice.my_conflicts() == bob.my_conflicts() == []
    assert [t.step_id for t in alice.tasks().on_my_requests] == ["welcome"]


def test_owner_catches_up_on_a_missed_join(pair):
    alice, bob = pair
    rid = approved(alice, bob)
    for c in alice.children(rid):
        state, defn = alice._load(c.request_id)
        alice._record(c.request_id, defn, engine.set_checklist_item(state, defn, "setup", 0, True), follow=False)
        state, defn = alice._load(c.request_id)
        alice._record(c.request_id, defn, engine.complete_step(state, defn, "setup"), follow=False)
    assert alice.get(rid)[0].step("accounts").status == engine.IN_PROGRESS
    alice.sync()  # the owner's poll completes the join
    assert alice.get(rid)[0].step("accounts").status == engine.DONE


def test_empty_fan_out_completes_straight_away(pair):
    alice, bob = pair
    rid = approved(alice, bob, accounts=[])
    state = alice.get(rid)[0]
    assert state.step("accounts").status == engine.DONE and state.step("accounts").data["children"] == []
    assert state.step("welcome").status == engine.READY


def test_child_with_incomplete_inputs_is_left_as_a_draft(pair):
    alice, bob = pair
    rid = approved(alice, bob, accounts=[{"account": "ACC1"}])  # no access level
    [child] = alice.children(rid)
    assert child.status == engine.DRAFT and child.step("capture").draft == {"account": "ACC1"}


def test_hold_and_resume_pass_to_children(pair):
    alice, bob = pair
    rid = approved(alice, bob)
    first, second = alice.children(rid)
    alice.start_step(first.request_id, "setup")
    alice.hold(rid, "Client paused onboarding")
    parent = alice.get(rid)[0]
    assert [(f.trigger, f.child) for f in parent.flags] == [("hold", first.request_id)]  # only the in-progress one
    held = [alice.get(c.request_id)[0] for c in (first, second)]
    assert all(c.status == engine.ON_HOLD and c.holds[-1].by_parent == rid for c in held)
    assert "Client paused onboarding" in held[0].holds[-1].reason
    alice.resume(rid)
    assert all(alice.get(c.request_id)[0].status == engine.ACTIVE for c in (first, second))
    assert alice.my_conflicts() == []


def test_resume_leaves_children_held_for_their_own_reasons(pair):
    alice, bob = pair
    rid = approved(alice, bob)
    first, _ = alice.children(rid)
    alice.hold(first.request_id, "Waiting for the client's account form")
    alice.hold(rid, "Client paused")
    alice.resume(rid)
    assert alice.get(first.request_id)[0].status == engine.ON_HOLD


def test_cancel_passes_to_children_and_flags_work_done(pair):
    alice, bob = pair
    rid = approved(alice, bob, accounts=ACCOUNTS + [{"account": "ACC3", "access": "trade"}])
    done, started, untouched = alice.children(rid)
    tick(alice, done.request_id, "setup")
    alice.start_step(started.request_id, "setup")
    alice.cancel(rid, "Client withdrew")
    parent = alice.get(rid)[0]
    child_flags = {f.child: f.action for f in parent.flags if f.child}
    assert set(child_flags) == {done.request_id, started.request_id}
    assert "was completed" in child_flags[done.request_id] and "work in progress" in child_flags[started.request_id]
    assert parent.status == engine.CANCELLING
    assert alice.get(done.request_id)[0].status == engine.COMPLETED  # can't cancel a finished one: flagged instead
    assert alice.get(untouched.request_id)[0].status == engine.CANCELLED
    assert alice.get(started.request_id)[0].status == engine.CANCELLING  # its own started step is flagged


def test_validation_of_loops_and_decision_options(tmp_path):
    from wft.validation import validate_repo
    child = copy.deepcopy(CHILD)
    child["steps"].append({"id": "back", "type": "subworkflow", "depends_on": ["setup"], "workflow": "demo_client"})
    errs = [i.message for i in validate_repo(write_defs(tmp_path / "loop", PARENT, child, files=FAMILY_FILES)).issues]
    assert any("start each other in a loop" in e for e in errs)
    parent = copy.deepcopy(PARENT)
    parent["steps"][1]["default"] = "welcome"
    errs = [i.message for i in validate_repo(write_defs(tmp_path / "opt", parent, CHILD, files=FAMILY_FILES)).errors]
    assert any("option 'welcome' must depend on the decision step" in e for e in errs)
