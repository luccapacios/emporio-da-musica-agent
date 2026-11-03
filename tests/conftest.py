import pytest

from emporio.config import ROOT
from emporio.data import load_store
from emporio.policy import Policies


@pytest.fixture(scope="session")
def policies():
    return Policies.load(ROOT / "data" / "processed" / "policies.md")


@pytest.fixture(scope="session")
def store():
    return load_store(ROOT / "data" / "raw")
