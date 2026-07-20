from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import app as app_module
import db


@pytest.fixture()
def client(tmp_path):
    db.configure(tmp_path / "audit.sqlite3", tmp_path / "evidence")
    db.seed()
    with TestClient(app_module.app) as test_client:
        yield test_client
    db.close()


@pytest.fixture()
def pending_confirmation():
    import db

    return db.query(
        "SELECT * FROM confirmations WHERE status='待发函' ORDER BY id LIMIT 1"
    )[0]
