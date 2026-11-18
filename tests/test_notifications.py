"""Notifications: what's worth telling, desktop toasts, unread counts and 'while you were away'."""
import pytest

from tests.conftest import DEMO, make_services
from wft import notifications
from wft.notifications import RecordingNotifier, WindowsToastNotifier

STEP = "do_the_thing"


def make(share, tmp_path, registry, user, home=None):
    svc = make_services(share, tmp_path / f"{user}.sqlite", registry, user)
    svc.notifier = RecordingNotifier()
    svc._acknowledged_path = tmp_path / f"{user}-ack.json"
    return svc


@pytest.fixture
def pair(share, tmp_path, registry):
    return make(share, tmp_path, registry, "alice"), make(share, tmp_path, registry, "bob")


def kinds(svc):
    return [(n.kind, n.text) for n in svc.notifier.shown]


def test_own_actions_never_notify(pair):
    alice, _ = pair
    rid = alice.create_request(DEMO, "Mine")
    alice.submit(rid, {})
    alice.claim_step(rid, STEP)
    alice.sync()
    assert alice.notifier.shown == [] and alice.unread_count() == 0


def test_step_ready_assignment_and_owner_changes(pair):
    alice, bob = pair
    rid = bob.create_request(DEMO, "Bob's request")
    bob.change_owner(rid, "alice")  # handed to alice while still a draft
    alice.sync()
    assert kinds(alice) == [("owner_changed", "bob made you the owner of Bob's request")]
    bob.submit(rid, {})  # bob submits: the first step becomes ready on alice's request
    alice.sync()
    assert kinds(alice)[-1] == ("step_ready", "“Do the thing” is ready")
    bob.assign_step(rid, STEP, "alice")
    alice.sync()
    assert kinds(alice)[-1] == ("step_assigned", "bob gave you “Do the thing”")
    assert alice.unread_count() == 3
    assert all(n.title.startswith("REQ-") for n in alice.notifier.shown[1:])


def test_lifecycle_changes_and_flags_on_my_request(share, tmp_path, krm_registry):
    alice, bob = make(share, tmp_path, krm_registry, "alice"), make(share, tmp_path, krm_registry, "bob")
    rid = alice.create_request("krm@1", "ACC1")
    alice.submit(rid, {"capture": {"account": "ACC1"}})
    alice.start_step(rid, "raise")
    bob.sync()
    bob.hold(rid, "client paused")
    alice.sync()
    got = kinds(alice)
    assert ("status_changed", "bob put on hold the request") in got
    assert any(k == "flag_raised" and "KRM22 may still approve ACC1" in t for k, t in got)
    bob.resume(rid)
    bob.cancel(rid, "withdrawn")
    alice.sync()
    texts = [t for _, t in kinds(alice)]
    assert "bob resumed the request" in texts and "bob cancelled the request" in texts
    assert sum("Withdraw the ACC1 request" in t for t in texts) == 1


def test_nothing_about_requests_i_dont_own(pair):
    alice, bob = pair
    rid = bob.create_request(DEMO, "Bob's")
    bob.submit(rid, {})
    bob.hold(rid, "x")
    alice.sync()
    assert alice.notifier.shown == []


def test_many_changes_make_one_summary_toast(pair):
    alice, bob = pair
    for i in range(6):
        rid = bob.create_request(DEMO, f"r{i}")
        bob.change_owner(rid, "alice")
    alice.sync()
    assert [n.kind for n in alice.notifier.shown] == ["summary"] and alice.unread_count() == 6


def test_while_you_were_away(share, tmp_path, registry):
    path = tmp_path / "last-online-uat.json"
    alice = make(share, tmp_path, registry, "alice")
    alice._last_online_path = path
    alice.sync()  # records when alice was last online
    alice.stop_polling()
    bob = make(share, tmp_path, registry, "bob")
    rid = bob.create_request(DEMO, "While away")
    bob.change_owner(rid, "alice")
    # Alice starts WFT again later, with a fresh cache.
    from wft.readmodel import ReadModel
    from wft.services import Services
    again = Services(alice.repo, ReadModel(tmp_path / "alice2.sqlite", registry), registry, "alice", "pc",
                     notifier=RecordingNotifier(), last_online_path=path)
    again.sync()
    assert [n.text for n in again.away] == ["bob made you the owner of While away"]
    again.sync()
    assert len(again.away) == 1  # only the first sync after start-up counts as "away"
    again.dismiss_away()
    assert again.away == []


def test_first_run_and_rebuild_dont_flood(share, tmp_path, registry):
    bob = make(share, tmp_path, registry, "bob")
    rid = bob.create_request(DEMO, "Old news")
    bob.change_owner(rid, "alice")
    alice = make(share, tmp_path, registry, "alice")  # first ever run: nothing from before counts
    assert alice.notifier.shown == [] and alice.away == []
    bob.submit(rid, {})
    alice.sync()
    assert len(alice.notifier.shown) == 1
    alice.rebuild_cache()
    assert len(alice.notifier.shown) == 1  # not announced again


def test_mark_read(pair):
    alice, bob = pair
    a = bob.create_request(DEMO, "a")
    b = bob.create_request(DEMO, "b")
    for rid in (a, b):
        bob.change_owner(rid, "alice")
    alice.sync()
    alice.mark_read(a)
    assert alice.unread_count() == 1 and len(alice.notices(unread_only=True)) == 1
    alice.mark_read()
    assert alice.unread_count() == 0 and len(alice.notices()) == 2


def test_windows_toast_passes_text_in_environment(monkeypatch):
    calls = []

    class FakePopen:
        def __init__(self, args, env, **kw):
            calls.append((args, env))
    monkeypatch.setattr(notifications.subprocess, "Popen", FakePopen)
    toast = WindowsToastNotifier("uat")
    evil = "x'); Remove-Item C:\\ -Recurse; ('"
    toast.show(notifications.Notice("r", "step_ready", evil, "body", "ts"))
    args, env = calls[0]
    assert args[0] == "powershell.exe" and evil not in " ".join(args)
    assert env["WFT_TOAST_TITLE"] == "WFT UAT · " + evil and env["WFT_TOAST_TEXT"] == "body"

    def broken(*a, **k):
        raise OSError("PowerShell blocked")
    monkeypatch.setattr(notifications.subprocess, "Popen", broken)
    toast = WindowsToastNotifier("prod")
    toast.show(notifications.Notice("r", "k", "t", "b", "ts"))
    toast.show(notifications.Notice("r", "k", "t", "b", "ts"))
    assert toast.broken


def test_notifier_choice_and_config(tmp_path, monkeypatch):
    from wft.config import ConfigError, load_config
    monkeypatch.setattr(notifications.sys, "platform", "win32")
    assert isinstance(notifications.make_notifier("", "uat"), WindowsToastNotifier)
    assert isinstance(notifications.make_notifier("off", "uat"), notifications.NullNotifier)
    monkeypatch.setattr(notifications.sys, "platform", "linux")
    assert isinstance(notifications.make_notifier("windows", "uat"), notifications.NullNotifier)
    (tmp_path / "uat.env").write_text("WFT_SHARE_ROOT=/s/uat\nWFT_NOTIFICATIONS=email\n")
    with pytest.raises(ConfigError, match="windows, browser or off"):
        load_config("uat", home=tmp_path)


def test_children_belong_to_the_parent_owner(share, tmp_path, family_registry):
    alice, bob = make(share, tmp_path, family_registry, "alice"), make(share, tmp_path, family_registry, "bob")
    rid = alice.create_request("demo_client@1", "Acme")
    alice.submit(rid, {"capture": {"client": "Acme", "accounts": [{"account": "A1", "access": "trade"}]}})
    alice.set_checklist_item(rid, "normal", 0, True)
    alice.complete_step(rid, "normal")
    bob.sync()
    bob.approve(rid, "signoff")
    [child] = bob.children(rid)
    assert child.owner == "alice"
    alice.sync()
    assert any(n.request_id == child.request_id and n.kind == "step_ready" for n in alice.notifier.shown)


def test_knock_on_changes_to_children_dont_notify_separately(share, tmp_path, family_registry):
    alice, bob = make(share, tmp_path, family_registry, "alice"), make(share, tmp_path, family_registry, "bob")
    rid = alice.create_request("demo_client@1", "Acme")
    alice.submit(rid, {"capture": {"client": "Acme", "accounts": [{"account": "A1", "access": "trade"},
                                                                   {"account": "A2", "access": "view"}]}})
    alice.set_checklist_item(rid, "normal", 0, True)
    alice.complete_step(rid, "normal")
    bob.sync()
    bob.approve(rid, "signoff")
    alice.sync()
    alice.notifier.shown.clear()
    bob.sync()
    bob.cancel(rid, "client withdrew")
    alice.sync()
    assert [t for k, t in kinds(alice) if k == "status_changed"] == ["bob cancelled the request"]
