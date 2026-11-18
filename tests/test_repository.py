import json
import os
import time

import pytest

from wft.events import Event
from wft.repository import FileRepository, InstanceLock, InstanceLockError, read_journal


def ev(i, request_id="r1"):
    return Event(event_id=f"e{i}", request_id=request_id, seq=i, type="t", actor="alice", host="h",
                 app_version="t", definition="d@1", ts=f"2026-11-18T10:00:0{i}.000+00:00", payload={"i": i})


def test_append_writes_to_users_monthly_journal(share):
    repo = FileRepository(share, "alice")
    repo.append(ev(1))
    repo.append(ev(2))
    path = repo.journal_path()
    assert path.parent.parent.name == "journals" and path.name == "alice.jsonl"
    assert [e.event_id for e in repo.read_all()] == ["e1", "e2"]


def test_each_user_writes_only_their_own_file(share):
    FileRepository(share, "alice").append(ev(1))
    FileRepository(share, "bob").append(ev(2, "r2"))
    repo = FileRepository(share, "carol")
    assert sorted(p.name for p in repo.journal_files()) == ["alice.jsonl", "bob.jsonl"]
    assert {e.event_id for e in repo.read_all()} == {"e1", "e2"}


def test_trailing_partial_line_is_ignored_until_complete(share):
    repo = FileRepository(share, "alice")
    repo.append(ev(1))
    path = repo.journal_path()
    line = ev(2).to_line()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[:20])
    assert [e.event_id for e in read_journal(path)] == ["e1"]
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[20:] + "\n")
    assert [e.event_id for e in read_journal(path)] == ["e1", "e2"]


def test_instance_lock_blocks_second_instance(share):
    path = share / "instance-locks" / "alice.lock"
    with InstanceLock(path, "alice", "pc1"):
        with pytest.raises(InstanceLockError, match="already running"):
            InstanceLock(path, "alice", "pc2").acquire()
    assert not path.exists()
    with InstanceLock(path, "alice", "pc2"):
        assert path.exists()


def test_stale_lock_is_taken_over(share):
    path = share / "instance-locks" / "alice.lock"
    path.parent.mkdir()
    path.write_text(json.dumps({"host": "crashed-pc", "pid": 1, "heartbeat": time.time() - 600}))
    with InstanceLock(path, "alice", "pc1", stale_after=120):
        assert json.loads(path.read_text())["host"] == "pc1"


def test_heartbeat_keeps_lock_fresh(share):
    path = share / "instance-locks" / "alice.lock"
    with InstanceLock(path, "alice", "pc1", heartbeat_every=0.05, stale_after=0.5):
        first = json.loads(path.read_text())["heartbeat"]
        time.sleep(0.2)
        assert json.loads(path.read_text())["heartbeat"] > first
        with pytest.raises(InstanceLockError):
            InstanceLock(path, "alice", "pc2", stale_after=0.5).acquire()


def test_unreadable_lock_falls_back_to_mtime(share):
    path = share / "instance-locks" / "alice.lock"
    path.parent.mkdir()
    path.write_text("{half")
    with pytest.raises(InstanceLockError):
        InstanceLock(path, "alice", "pc1").acquire()
    old = time.time() - 600
    os.utime(path, (old, old))
    with InstanceLock(path, "alice", "pc1"):
        pass


def test_read_since_returns_only_new_complete_lines(share):
    alice = FileRepository(share, "alice")
    reader = FileRepository(share, "bob")
    alice.append(ev(1))
    events, cursor = reader.read_since({})
    assert [e.event_id for e in events] == ["e1"]
    assert reader.read_since(cursor) == ([], cursor)

    alice.append(ev(2))
    path = alice.journal_path()
    line = ev(3).to_line()
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[:15])  # alice's next write is still in progress
    events, cursor = reader.read_since(cursor)
    assert [e.event_id for e in events] == ["e2"]
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[15:] + "\n")
    events, cursor = reader.read_since(cursor)
    assert [e.event_id for e in events] == ["e3"]
    assert cursor[f"{path.parent.name}/alice.jsonl"] == path.stat().st_size


def test_cut_off_write_does_not_corrupt_later_events(share):
    repo = FileRepository(share, "alice")
    repo.append(ev(1))
    with open(repo.journal_path(), "a", encoding="utf-8") as f:
        f.write('{"event_id": "half')  # network dropped mid-append
    repo.append(ev(2))
    assert [e.event_id for e in repo.read_all()] == ["e1", "e2"]


def test_unreachable_share(tmp_path):
    from wft.repository import ShareUnavailable
    repo = FileRepository(tmp_path / "gone", "alice")
    with pytest.raises(ShareUnavailable):
        repo.read_since({})
    with pytest.raises(ShareUnavailable):
        repo.append(ev(1))
    assert not (tmp_path / "gone").exists()  # never recreated locally
