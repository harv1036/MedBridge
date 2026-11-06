import pytest
from fastapi.testclient import TestClient

import seed
from app import db
from app.main import app


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """A freshly seeded database in a temp folder, so tests never touch the demo data."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    seed.reset()


@pytest.fixture
def client(seeded):
    return TestClient(app)
