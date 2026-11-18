"""Ownership, step claims and assignment, My tasks, and team roles."""
import os
import time

import pytest
import yaml

from tests.conftest import DEMO, make_services
from tests.defs import MANUAL, write_defs
from wft import engine
from wft.engine import CommandError
from wft.publishing import PublishError, publish
from wft.team import ROLES_FILE, TeamSource, load_team, parse_team

STEP = "do_the_thing"


@pytest.fixture
def pair(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    bob = make_services(share, tmp_path / "b.sqlite", registry, "bob")
    alice._acknowledged_path = tmp_path / "a-ack.json"
    bob._acknowledged_path = tmp_path / "b-ack.json"
    return alice, bob


def active(svc, title="r"):
    rid = svc.create_request(DEMO, title)
    svc.submit(rid, {})
    return rid


def test_owner_defaults_to_creator_and_reassignment_is_recorded(pair):
    alice, bob = pair
    rid = active(alice)
    assert alice.get(rid)[0].owner == "alice"
    bob.sync()
    bob.change_owner(rid, "Carol")  # any operator can reassign; usernames are case-insensitive
    alice.sync()
    assert alice.get(rid)[0].owner == "carol"
    [e] = [e for e in alice.rm.history(rid) if e.type == engine.OWNER_CHANGED]
    assert e.actor == "bob" and e.payload == {"owner": "carol", "previous": "alice"}
    with pytest.raises(CommandError, match="already owns"):
        alice.change_owner(rid, "carol")


def test_claim_assign_release(pair):
    alice, bob = pair
    rid = active(alice)
    alice.claim_step(rid, STEP)
    assert alice.get(rid)[0].step(STEP).assignee == "alice"
    with pytest.raises(CommandError, match="already have"):
        alice.claim_step(rid, STEP)
    bob.sync()
    with pytest.raises(CommandError, match="alice has this step"):
        bob.claim_step(rid, STEP)
    bob.assign_step(rid, STEP, "bob")  # explicit reassignment overrides
    alice.sync()
    assert alice.get(rid)[0].step(STEP).assignee == "bob"
    alice.release_step(rid, STEP)
    bob.sync()
    assert bob.get(rid)[0].step(STEP).assignee is None
    with pytest.raises(CommandError, match="Nobody"):
        bob.release_step(rid, STEP)


def test_only_ready_steps_can_be_claimed(share, tmp_path, registry):
    svc = make_services(share, tmp_path / "c.sqlite", registry)
    rid = svc.create_request(DEMO, "draft")
    with pytest.raises(CommandError, match="Draft"):
        svc.claim_step(rid, STEP)


def test_simultaneous_claims_first_wins_and_loser_is_told(pair):
    alice, bob = pair
    rid = active(alice)
    bob.sync()
    alice.claim_step(rid, STEP)
    bob.claim_step(rid, STEP)  # bob hadn't seen alice's claim
    alice.sync()
    bob.sync()
    assert alice.get(rid)[0].step(STEP).assignee == bob.get(rid)[0].step(STEP).assignee == "alice"
    [conflict] = bob.my_conflicts()
    assert conflict.type == engine.STEP_ASSIGNMENT and alice.my_conflicts() == []


def test_assignment_never_conflicts_with_ticks(pair):
    alice, bob = pair
    rid = active(alice)
    bob.sync()
    alice.set_checklist_item(rid, STEP, 0, True)
    bob.claim_step(rid, STEP)
    alice.sync()
    bob.sync()
    state = alice.get(rid)[0].step(STEP)
    assert state.ticked == {0} and state.assignee == "bob"
    assert alice.my_conflicts() == bob.my_conflicts() == []


def test_my_tasks_sections(pair):
    alice, bob = pair
    mine_claimed = active(alice, "claimed by me")
    alice.claim_step(mine_claimed, STEP)
    mine_open = active(alice, "my unclaimed")
    others = active(bob, "bob's")
    bobs_assigned_to_alice = active(bob, "bob's, given to alice")
    bob.assign_step(bobs_assigned_to_alice, STEP, "alice")
    taken = active(bob, "bob has it")
    bob.claim_step(taken, STEP)
    draft = alice.create_request(DEMO, "my draft")
    alice.sync()

    t = alice.tasks()
    titles = lambda ts: sorted(x.request.title for x in ts)  # noqa: E731
    assert titles(t.mine) == ["bob's, given to alice", "claimed by me"]
    assert titles(t.on_my_requests) == ["my unclaimed"]
    assert titles(t.claimable) == ["bob's"]
    assert [r.request_id for r in t.drafts] == [draft]
    assert all(x.step_title == "Do the thing" for x in t.mine)
    assert others and taken


def test_roles_file_parsing_and_errors():
    team = parse_team({"users": [{"username": "JSmith", "name": "Jane Smith", "roles": ["admin"]},
                                 {"username": "akhan"}]})
    assert team.is_admin("jsmith") and not team.is_admin("akhan") and not team.is_admin("nobody")
    assert team.name("JSMITH") == "Jane Smith" and team.name("akhan") == "akhan" and team.name("x") == "x"
    assert team.roles("jsmith") == ["operator", "admin"] and team.roles("x") == ["operator"]
    with pytest.raises(ValueError, match="'superuser' is not one of"):
        parse_team({"users": [{"username": "a", "roles": ["superuser"]}]})
    with pytest.raises(ValueError, match="listed more than once"):
        parse_team({"users": [{"username": "a"}, {"username": "A"}]})


def test_team_source_reloads_and_keeps_last_good(tmp_path):
    path = tmp_path / "roles.yaml"
    src = TeamSource(path)
    assert src.team.admins == []
    path.write_text(yaml.safe_dump({"users": [{"username": "jsmith", "roles": ["admin"]}]}))
    assert src.refresh() and src.team.admins == ["jsmith"]
    assert not src.refresh()
    path.write_text("users: [{roles: [god]}]")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert not src.refresh() and src.team.admins == ["jsmith"]


def roles(*admins, others=()):
    return {"users": [{"username": a, "roles": ["admin"]} for a in admins] + [{"username": o} for o in others]}


def test_publishing_roles_and_admin_only_publishing(tmp_path, share):
    defs = write_defs(tmp_path / "d", MANUAL, files={"config/roles.yaml": roles("alice", others=["bob"])})
    # Bootstrap: no roles published yet, so anyone may publish; the roles file is published too.
    result = publish(defs, share, "alice@pc", username="alice")
    assert "roles" in result.published and load_team(share / ROLES_FILE).admins == ["alice"]
    assert "roles" in publish(defs, share, "alice@pc", username="alice").unchanged
    with pytest.raises(PublishError, match="Only admins can publish"):
        publish(defs, share, "bob@pc", username="bob")


def test_cannot_publish_roles_that_lock_yourself_out(tmp_path, share):
    defs = write_defs(tmp_path / "d", MANUAL, files={"config/roles.yaml": roles("carol")})
    with pytest.raises(PublishError, match="locked out"):
        publish(defs, share, "alice@pc", username="alice")
    assert not (share / ROLES_FILE).exists()


def test_invalid_roles_file_fails_validation(tmp_path, share):
    from wft.validation import validate_repo
    defs = write_defs(tmp_path / "d", MANUAL, files={"config/roles.yaml": {"users": [{"name": "no username"}]}})
    assert any("'username' is a required property" in i.message for i in validate_repo(defs).errors)
    with pytest.raises(PublishError, match="Validation failed"):
        publish(defs, share, "alice@pc", username="alice")


def test_services_pick_up_team_changes(share, tmp_path, registry):
    path = share / ROLES_FILE
    svc = make_services(share, tmp_path / "c.sqlite", registry)
    svc.team_source = TeamSource(path)
    assert not svc.is_admin
    path.parent.mkdir(parents=True)
    path.write_text(yaml.safe_dump(roles("alice", others=["bob"]) | {"users": [
        {"username": "alice", "name": "Alice A", "roles": ["admin"]}, {"username": "bob", "name": "Bob B"}]}))
    svc.sync()
    assert svc.is_admin and svc.team.name("bob") == "Bob B"
    assert ("bob", "Bob B") in svc.people() and ("alice", "Alice A") in svc.people()
