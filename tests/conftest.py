from pathlib import Path

import pytest

from wft.config import REPO_ROOT
from wft.definitions import DefinitionRegistry, parse_workflow
from wft.readmodel import ReadModel
from wft.engine import MAX_ATTACHMENT_BYTES
from wft.repository import AttachmentStore, FileRepository, ReferenceAllocator
from wft.services import Services

DEMO = "demo_manual@1"


@pytest.fixture
def registry() -> DefinitionRegistry:
    return DefinitionRegistry.from_repo(REPO_ROOT / "definitions")


@pytest.fixture
def share(tmp_path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    return root


def make_services(share: Path, cache: Path, registry, user: str = "alice") -> Services:
    rm = ReadModel(cache, registry)
    svc = Services(FileRepository(share, user), rm, registry, user, "host-" + user,
                   references=ReferenceAllocator(share),
                   attachments=AttachmentStore(share, MAX_ATTACHMENT_BYTES))
    svc.sync()
    return svc


@pytest.fixture
def services(share, tmp_path, registry) -> Services:
    return make_services(share, tmp_path / "cache" / "uat.sqlite", registry)


@pytest.fixture
def two_step() -> DefinitionRegistry:
    return DefinitionRegistry([parse_workflow({
        "id": "two", "version": 1, "title": "Two steps",
        "steps": [
            {"id": "a", "type": "manual", "checklist": ["x"]},
            {"id": "b", "type": "manual", "depends_on": ["a"]},
        ],
    })])


@pytest.fixture(autouse=True)
def ticking_clock(monkeypatch):
    """Strictly increasing timestamps, so 'who acted first' in tests never depends on the millisecond."""
    from datetime import datetime, timedelta, timezone

    import wft.services
    start = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
    counter = iter(range(10**9))
    monkeypatch.setattr(wft.services, "utc_now",
                        lambda: (start + timedelta(milliseconds=next(counter))).isoformat(timespec="milliseconds"))


@pytest.fixture
def forms_registry(tmp_path) -> DefinitionRegistry:
    from tests.defs import FORMS, FORMS_FILES, write_defs
    return DefinitionRegistry.from_repo(write_defs(tmp_path / "forms-defs", FORMS, files=FORMS_FILES))


@pytest.fixture
def krm_registry(tmp_path) -> DefinitionRegistry:
    from tests.defs import LIFECYCLE, LIFECYCLE_FILES, write_defs
    return DefinitionRegistry.from_repo(write_defs(tmp_path / "krm-defs", LIFECYCLE, files=LIFECYCLE_FILES))


@pytest.fixture
def amend_registry(tmp_path) -> DefinitionRegistry:
    from tests.defs import AMENDABLE, AMENDABLE_FILES, write_defs
    return DefinitionRegistry.from_repo(write_defs(tmp_path / "amend-defs", AMENDABLE, files=AMENDABLE_FILES))
