import pytest

from emporio.config import ROOT
from emporio.policy import Policies


@pytest.fixture(scope="session")
def policies():
    return Policies.load(ROOT / "data" / "processed" / "policies.md")
