from pathlib import Path

import pytest

from wft.config import REPO_ROOT
from wft.definitions import DefinitionRegistry, parse_workflow
from wft.readmodel import ReadModel
from wft.repository import FileRepository
from wft.services import Services

DEMO = "demo_manual@1"


@pytest.fixture
def registry() -> DefinitionRegistry:
    return DefinitionRegistry.load(REPO_ROOT / "definitions")


@pytest.fixture
def share(tmp_path) -> Path:
    root = tmp_path / "share"
    root.mkdir()
    return root


def make_services(share: Path, cache: Path, registry, user: str = "alice") -> Services:
    rm = ReadModel(cache, registry)
    svc = Services(FileRepository(share, user), rm, registry, user, "host-" + user)
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
