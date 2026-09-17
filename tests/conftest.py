"""Shared pytest fixtures.

By default tests hit the FastAPI app in-process through FastAPI's
TestClient (no network). When BASE_URL is set, as in the one-shot compose
`verify` service, they run as black-box HTTP checks against a live API:

    BASE_URL=http://api:8000 pytest -q
"""

from __future__ import annotations

import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    base_url = os.environ.get("BASE_URL")
    if base_url:
        with httpx.Client(base_url=base_url, timeout=10) as live:
            yield live
        return
    # Isolate every in-process test with its own SQLite file; the app
    # reads UPLOADS_DB_PATH when its lifespan starts.
    monkeypatch.setenv("UPLOADS_DB_PATH", str(tmp_path / "uploads.db"))
    with TestClient(app) as in_process:
        yield in_process
