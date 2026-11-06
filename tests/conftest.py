"""Shared harness: one real uvicorn process on a temp database, reset before each test."""
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import db

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_LINES = []


def local_client(url, **options):
    """A client for plain http on localhost. verify=False skips building TLS settings it never uses (about 370 ms each)."""
    return httpx.Client(base_url=url, verify=False, timeout=30, **options)


def wait_until_up(url, process):
    deadline = time.monotonic() + 20
    with local_client(url) as probe:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"uvicorn exited with code {process.returncode}")
            try:
                probe.get("/openapi.json")
                return
            except httpx.TransportError:
                time.sleep(0.05)
    raise RuntimeError("uvicorn did not start in time")


@contextmanager
def running_server(db_path):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env={**os.environ, "MEDBRIDGE_DB": str(db_path)},
    )
    url = f"http://127.0.0.1:{port}"
    try:
        wait_until_up(url, process)
        yield SimpleNamespace(url=url, db_path=db_path, client=lambda **options: local_client(url, **options))
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="session")
def server(tmp_path_factory):
    with running_server(tmp_path_factory.mktemp("db") / "test.db") as running:
        yield running


@pytest.fixture(scope="session")
def second_server(server):
    """A second process on the same database, to prove writes are safe beyond one process."""
    with running_server(server.db_path) as running:
        yield running


@pytest.fixture
def seeded(server, monkeypatch):
    """Reset through the real endpoint, and point this process at the same database for direct checks."""
    monkeypatch.setattr(db, "DB_PATH", server.db_path)
    with server.client() as http_client:
        response = http_client.post("/admin/reset")
    assert response.status_code == 200, response.text


@pytest.fixture
def client(server, seeded):
    with server.client(headers={"X-Actor": "pharmacist_a"}) as http_client:
        yield http_client


@pytest.fixture
def summary():
    """Record a line for the numbers section printed after the test run."""
    return SUMMARY_LINES.append


def pytest_terminal_summary(terminalreporter):
    if SUMMARY_LINES:
        terminalreporter.section("numbers")
        for line in SUMMARY_LINES:
            terminalreporter.write_line(line)
