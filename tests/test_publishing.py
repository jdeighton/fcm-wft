import copy
import json

import pytest

from tests.conftest import make_services
from tests.defs import MANUAL, write_defs
from wft.definitions import DefinitionRegistry, published_version_dirs
from wft.engine import CommandError
from wft.publishing import PublishError, publish
from wft.readmodel import ReadModel
from wft.repository import FileRepository
from wft.services import Services


def defs(tmp_path, **changes):
    d = copy.deepcopy(MANUAL)
    d.update(changes)
    return write_defs(tmp_path / "defs", d, files={"checklists/a.md": "- From markdown\n"})


def test_publish_writes_an_immutable_version_folder(tmp_path, share):
    root = defs(tmp_path)
    result = publish(root, share, "alice@pc", commit="abc123")
    assert result.published == ["simple@1"]
    [folder] = published_version_dirs(share)
    assert folder.relative_to(share).as_posix() == "definitions/workflows/simple/1"
    manifest = json.loads((folder / "manifest.json").read_text())
    assert manifest["published_by"] == "alice@pc" and manifest["git_commit"] == "abc123"
    assert set(manifest["files"]) == {"workflow.yaml"}
    assert not list((share / "definitions" / ".staging").glob("*"))

    again = publish(root, share, "alice@pc")
    assert again.published == [] and again.unchanged == ["simple@1"]


def test_publish_copies_referenced_files(tmp_path, share):
    d = copy.deepcopy(MANUAL)
    d["steps"][0]["checklist"] = "a.md"
    root = write_defs(tmp_path / "defs", d, files={"checklists/a.md": "- From markdown\n", "checklists/unused.md": "- x\n"})
    publish(root, share, "alice@pc")
    folder = share / "definitions/workflows/simple/1"
    assert (folder / "checklists/a.md").read_text() == "- From markdown\n"
    assert not (folder / "checklists/unused.md").exists()
    reg = DefinitionRegistry.from_share(share)
    assert reg.get("simple@1").step("a").checklist == ("From markdown",)


def test_changing_a_published_version_without_bumping_is_refused(tmp_path, share):
    root = defs(tmp_path)
    publish(root, share, "alice@pc")
    changed = defs(tmp_path, title="Simple, renamed")
    with pytest.raises(PublishError) as exc:
        publish(changed, share, "alice@pc")
    assert any("bump the version" in i.message for i in exc.value.report.errors)
    assert published_version_dirs(share)[0].name == "1"
    assert publish(defs(tmp_path, title="Simple, renamed", version=2), share, "alice@pc").published == ["simple@2"]


def test_going_back_to_an_older_version_number_is_refused(tmp_path, share):
    publish(defs(tmp_path, version=3), share, "alice@pc")
    with pytest.raises(PublishError) as exc:
        publish(defs(tmp_path, version=2), share, "alice@pc")
    assert any("older than the published version 3" in i.message for i in exc.value.report.errors)


def test_invalid_definitions_publish_nothing(tmp_path, share):
    d = copy.deepcopy(MANUAL)
    d["steps"][0]["depends_on"] = ["ghost"]
    with pytest.raises(PublishError, match="nothing was published"):
        publish(write_defs(tmp_path / "defs", d), share, "alice@pc")
    assert published_version_dirs(share) == []


def test_half_written_versions_are_ignored(tmp_path, share):
    publish(defs(tmp_path), share, "alice@pc")
    partial = share / "definitions/workflows/simple/2"
    partial.mkdir()
    (partial / "workflow.yaml").write_text("id: simple\n")  # no manifest yet
    assert DefinitionRegistry.from_share(share).keys() == ["simple@1"]


def test_requests_stay_on_the_version_they_started_on(tmp_path, share):
    publish(defs(tmp_path), share, "alice@pc")
    registry = DefinitionRegistry.from_share(share)
    svc = Services(FileRepository(share, "alice"), ReadModel(tmp_path / "c.sqlite", registry), registry, "alice", "pc")
    old = svc.create_request("simple@1", "Started on v1")
    svc.submit(old, {})

    v2 = copy.deepcopy(MANUAL)
    v2.update(version=2, title="Simple v2")
    v2["steps"].append({"id": "c", "type": "manual", "depends_on": ["b"], "checklist": ["three"]})
    publish(write_defs(tmp_path / "defs2", v2), share, "alice@pc")
    svc.sync()  # picks up the new version without a restart

    assert [w.key for w in registry.latest()] == ["simple@2"]
    new = svc.create_request(registry.latest()[0].key, "Started on v2")
    svc.submit(new, {})
    assert svc.get(old)[1].key == "simple@1" and [s.id for s in svc.get(old)[0].steps] == ["a", "b"]
    assert [s.id for s in svc.get(new)[0].steps] == ["a", "b", "c"]
    for step in ("a", "b"):
        svc.set_checklist_item(old, step, 0, True)
        svc.complete_step(old, step)
    assert svc.get(old)[0].status == "Completed"


def test_requests_for_definitions_not_loaded_yet_are_projected_later(tmp_path, share, registry):
    alice = make_services(share, tmp_path / "a.sqlite", registry, "alice")
    rid = alice.create_request("demo_manual@1", "x")
    alice.submit(rid, {})
    empty = DefinitionRegistry()
    bob = Services(FileRepository(share, "bob"), ReadModel(tmp_path / "b.sqlite", empty), empty, "bob", "pc")
    bob.sync()
    assert bob.rm.get(rid) is None  # bob doesn't know the definition yet
    empty._add(registry.latest())
    bob.sync()
    assert bob.rm.get(rid).title == "x"


def test_older_app_will_not_start_requests_on_newer_definitions(tmp_path, share):
    publish(defs(tmp_path, min_app_version="99.0.0"), share, "alice@pc")
    registry = DefinitionRegistry.from_share(share)
    svc = Services(FileRepository(share, "alice"), ReadModel(tmp_path / "c.sqlite", registry), registry, "alice", "pc")
    with pytest.raises(CommandError, match="needs WFT 99.0.0"):
        svc.create_request("simple@1", "x")


def test_ui_marks_workflows_needing_a_newer_app(tmp_path, share):
    from tests.test_web import client_for
    publish(defs(tmp_path, min_app_version="99.0.0"), share, "alice@pc")
    registry = DefinitionRegistry.from_share(share)
    svc = Services(FileRepository(share, "alice"), ReadModel(tmp_path / "c.sqlite", registry), registry, "alice", "pc")
    page = client_for(svc).get("/").text
    assert "needs WFT 99.0.0" in page and "disabled" in page


def test_scripts(tmp_path, share, monkeypatch, capsys):
    import scripts.publish_definitions as pub
    import scripts.validate as val
    (share / "meta").mkdir()
    (share / "meta" / "environment").write_text("uat")
    uat_share = share.parent / "uat"
    share.rename(uat_share)
    home = tmp_path / "home"
    home.mkdir()
    (home / "uat.env").write_text(f"WFT_SHARE_ROOT={uat_share}\n")
    monkeypatch.setenv("WFT_HOME", str(home))
    root = defs(tmp_path)

    assert val.main(["--definitions", str(root), "--env", "uat"]) == 0
    assert pub.main(["--env", "uat", "--definitions", str(root)]) == 0
    assert "published  simple@1" in capsys.readouterr().out
    assert pub.main(["--env", "uat", "--definitions", str(root)]) == 0
    assert "unchanged  simple@1" in capsys.readouterr().out
    bad = defs(tmp_path, title="changed")
    assert val.main(["--definitions", str(bad), "--env", "uat"]) == 1
    assert pub.main(["--env", "uat", "--definitions", str(bad)]) == 1
    assert "bump the version" in capsys.readouterr().err
