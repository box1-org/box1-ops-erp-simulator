import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def contracts_dir() -> Path:
    """ERP1_CONTRACTS_DIR overrides the pinned checkout made by scripts/fetch-contracts.sh."""
    return Path(os.environ.get("ERP1_CONTRACTS_DIR", ROOT / ".contracts"))


def fixtures_dir() -> Path:
    return contracts_dir() / "legacy" / "erp-file" / "v1" / "fixtures"


@pytest.fixture(scope="session")
def manifest():
    path = fixtures_dir() / "manifest.json"
    if not path.exists():
        pytest.fail(f"ERP1 fixtures not found at {path}; run scripts/fetch-contracts.sh")
    return json.loads(path.read_text())


@pytest.fixture(scope="session")
def fixture_bytes():
    return lambda rel: (fixtures_dir() / rel).read_bytes()
