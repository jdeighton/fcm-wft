import hashlib
import io

import pytest

from tests.conftest import DEMO, make_services
from wft import engine
from wft.engine import CommandError
from wft.repository import AttachmentStore, AttachmentTooLarge, ShareUnavailable, safe_filename
from wft.services import NotFound, ReadOnly

RID = "0" * 32
PDF = b"%PDF-1.7 signed form" * 100


def test_store_writes_once_with_hash_in_the_name(share):
    store = AttachmentStore(share, 10_000)
    stored = store.put(RID, "Signed form (v2).pdf", io.BytesIO(PDF))
    sha = hashlib.sha256(PDF).hexdigest()
    assert stored.sha256 == sha and stored.size == len(PDF)
    assert stored.path == f"attachments/{RID}/{sha[:16]}_Signed_form_v2.pdf"
    path = share / stored.path
    assert path.read_bytes() == PDF
    mtime = path.stat().st_mtime_ns
    again = store.put(RID, "Signed form (v2).pdf", io.BytesIO(PDF))
    assert again == stored and path.stat().st_mtime_ns == mtime  # never rewritten
    assert not list((share / "attachments" / RID).glob("*.tmp"))


def test_too_large_is_refused_and_leaves_nothing(share):
    store = AttachmentStore(share, 1000)
    with pytest.raises(AttachmentTooLarge, match="larger than the 0 MB limit|limit"):
        store.put(RID, "big.bin", io.BytesIO(b"x" * 1001))
    assert list((share / "attachments" / RID).iterdir()) == []
    assert store.put(RID, "ok.bin", io.BytesIO(b"x" * 1000)).size == 1000


def test_store_refuses_bad_ids_paths_and_missing_share(share, tmp_path):
    store = AttachmentStore(share, 1000)
    with pytest.raises(ValueError):
        store.put("../../etc", "x", io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        store.open_path("journals/2026-11/alice.jsonl")
    with pytest.raises(ValueError):
        store.open_path("attachments/../meta/environment")
    with pytest.raises(ShareUnavailable):
        AttachmentStore(tmp_path / "gone", 1000).put(RID, "x", io.BytesIO(b"x"))


@pytest.mark.parametrize("name, safe", [
    ("Signed form (v2).PDF", "Signed_form_v2.PDF"), (r"C:\Users\x\mail.msg", "mail.msg"),
    ("../../etc/passwd", "passwd"), ("...", "file"), ("no-ext", "no-ext"),
])
def test_safe_filename(name, safe):
    assert safe_filename(name) == safe


def test_attach_list_and_download_via_services(services, share):
    rid = services.create_request(DEMO, "x")
    sha = services.add_attachment(rid, "client email.msg", io.BytesIO(b"From: client"))
    services.add_attachment(rid, "evidence.png", io.BytesIO(b"png"), step_id="do_the_thing")
    state, _ = services.get(rid)
    assert [(a.name, a.size, a.actor, a.step) for a in state.attachments] == [
        ("client email.msg", 12, "alice", None), ("evidence.png", 3, "alice", "do_the_thing")]
    att, path = services.attachment_file(rid, sha)
    assert path.read_bytes() == b"From: client" and att.sha256 == hashlib.sha256(b"From: client").hexdigest()
    [event] = [e for e in services.rm.history(rid) if e.type == engine.ATTACHMENT_ADDED and e.payload["step"] is None]
    assert event.payload["sha256"] == sha and event.payload["size"] == 12 and event.actor == "alice" and event.ts
    with pytest.raises(CommandError, match="already attached"):
        services.add_attachment(rid, "client email.msg", io.BytesIO(b"From: client"))
    with pytest.raises(CommandError, match="Unknown step"):
        services.add_attachment(rid, "x.txt", io.BytesIO(b"x"), step_id="nope")
    with pytest.raises(NotFound):
        services.attachment_file(rid, "f" * 64)


def test_attachments_never_conflict_and_survive_rebuild(share, tmp_path, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    bob = make_services(share, tmp_path / "b.sqlite", registry, "bob")
    rid = alice.create_request(DEMO, "x")
    alice.submit(rid, {})
    bob.sync()
    alice.set_checklist_item(rid, "do_the_thing", 0, True)
    bob.add_attachment(rid, "from bob.pdf", io.BytesIO(b"bob"))
    alice.sync()
    state = alice.get(rid)[0]
    assert state.step("do_the_thing").ticked == {0} and [a.name for a in state.attachments] == ["from bob.pdf"]
    assert alice.my_conflicts() == bob.my_conflicts() == []
    alice.rebuild_cache()
    assert [a.name for a in alice.get(rid)[0].attachments] == ["from bob.pdf"]


def test_missing_file_on_share_gives_a_clear_error(services, share):
    rid = services.create_request(DEMO, "x")
    sha = services.add_attachment(rid, "gone.txt", io.BytesIO(b"x"))
    (share / services.get(rid)[0].attachment(sha).path).unlink()
    with pytest.raises(CommandError, match="isn't on the shared drive"):
        services.attachment_file(rid, sha)


def test_read_only_blocks_attaching(services, share):
    rid = services.create_request(DEMO, "x")
    hidden = share.with_name("offline")
    share.rename(hidden)
    try:
        services.sync()
        with pytest.raises(ReadOnly):
            services.add_attachment(rid, "x.txt", io.BytesIO(b"x"))
    finally:
        hidden.rename(share)


def test_too_large_upload_through_services(services):
    services.attachments.max_bytes = 10
    rid = services.create_request(DEMO, "x")
    with pytest.raises(CommandError, match="limit"):
        services.add_attachment(rid, "big.bin", io.BytesIO(b"x" * 11))
    assert services.get(rid)[0].attachments == ()
