"""Two users' instances against one share: syncing, conflicts, read-only mode and cache rebuild."""
import threading

import pytest

from tests.conftest import DEMO, make_services
from wft.services import ReadOnly


@pytest.fixture
def pair(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    bob = make_services(share, tmp_path / "b.sqlite", registry, "bob")
    alice._acknowledged_path = tmp_path / "alice-ack.json"
    bob._acknowledged_path = tmp_path / "bob-ack.json"
    return alice, bob


def test_concurrent_writers_converge(pair):
    alice, bob = pair
    rid = alice.create_request(DEMO, "Shared")
    bob.sync()
    errors = []

    def tick(svc, index):
        try:
            svc.set_checklist_item(rid, "do_the_thing", index, True)
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=tick, args=(alice, 0)), threading.Thread(target=tick, args=(bob, 1))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    alice.sync()
    bob.sync()
    assert alice.rm.dump() == bob.rm.dump()
    ticked = alice.get(rid)[0].step("do_the_thing").ticked
    # Both acted on seq 1, so exactly one tick survives and the other author is told.
    assert len(ticked) == 1
    losers = alice.my_conflicts() + bob.my_conflicts()
    assert len(losers) == 1 and losers[0].payload["index"] not in ticked


def test_losing_author_sees_conflict_until_dismissed_even_after_rebuild(pair):
    alice, bob = pair
    rid = alice.create_request(DEMO, "Shared")
    bob.sync()
    alice.set_checklist_item(rid, "do_the_thing", 0, True)
    bob.set_checklist_item(rid, "do_the_thing", 1, True)  # bob hadn't seen alice's tick
    bob.sync()
    alice.sync()
    assert alice.my_conflicts() == []
    [conflict] = bob.my_conflicts()
    assert conflict.payload["index"] == 1
    assert bob.get(rid)[0].step("do_the_thing").ticked == {0}

    bob.acknowledge_conflict(conflict.event_id)
    assert bob.my_conflicts() == []
    bob.rebuild_cache()
    assert bob.my_conflicts() == []
    # Retrying on the up-to-date state works.
    bob.set_checklist_item(rid, "do_the_thing", 1, True)
    alice.sync()
    assert alice.get(rid)[0].step("do_the_thing").ticked == {0, 1}


def test_share_loss_goes_read_only_and_recovers(pair, share):
    alice, _ = pair
    rid = alice.create_request(DEMO, "Before outage")
    hidden = share.with_name("share-offline")
    share.rename(hidden)
    try:
        assert alice.sync() is False
        status = alice.status()
        assert status.read_only and status.since
        assert alice.get(rid)[0].title == "Before outage"  # last known state still shown
        with pytest.raises(ReadOnly):
            alice.set_checklist_item(rid, "do_the_thing", 0, True)
        with pytest.raises(ReadOnly):
            alice.create_request(DEMO, "During outage")
        with pytest.raises(ReadOnly):
            alice.rebuild_cache()
    finally:
        hidden.rename(share)
    assert alice.sync() is True
    assert not alice.status().read_only
    alice.set_checklist_item(rid, "do_the_thing", 0, True)


def test_share_lost_during_write_goes_read_only(pair, monkeypatch):
    alice, _ = pair
    rid = alice.create_request(DEMO, "x")

    def broken(event):
        raise OSError("The network path was not found")
    monkeypatch.setattr(alice.repo, "append", broken)
    with pytest.raises(ReadOnly, match="not saved"):
        alice.set_checklist_item(rid, "do_the_thing", 0, True)
    assert alice.status().read_only
    assert alice.get(rid)[0].step("do_the_thing").ticked == frozenset()


def test_poller_picks_up_other_users_changes(pair):
    alice, bob = pair
    bob.start_polling(every=0.05)
    try:
        rid = alice.create_request(DEMO, "Polled")
        for _ in range(100):
            if bob.rm.get(rid):
                break
            threading.Event().wait(0.02)
        assert bob.rm.get(rid).title == "Polled"
    finally:
        bob.stop_polling()


def test_sync_is_incremental(pair):
    alice, bob = pair
    alice.create_request(DEMO, "One")
    bob.sync()
    cursor = bob.rm.cursor()
    assert cursor and all(v > 0 for v in cursor.values())
    calls = []
    original = bob.repo.read_since
    bob.repo.read_since = lambda c: calls.append(dict(c)) or original(c)
    bob.sync()
    assert calls == [cursor]


def test_old_cache_versions_are_rebuilt(share, tmp_path, registry):
    import sqlite3
    cache = tmp_path / "old.sqlite"
    db = sqlite3.connect(cache)
    db.execute("CREATE TABLE requests (request_id TEXT, state_json TEXT)")
    db.execute("INSERT INTO requests VALUES ('x', '{}')")
    db.commit()
    db.close()
    svc = make_services(share, cache, registry)
    assert svc.rm.list_requests() == []
