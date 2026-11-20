"""Serve's provider-path lock routes (hermes_cli/web_routers/provider_locks.py).

hermes-serve owns the lifecycle surface for locked provider paths. These exercise the two routes
against the same on-disk state every other process reads, so the dashboard can never show a lock
that is not there — or hide one that is.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.provider_locks import clear_lock, create_lock, list_locks
from hermes_cli.web_routers.provider_locks import router


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_nothing_locked_reads_as_an_empty_list(client):
    assert client.get("/api/provider-locks").json() == {"locked_paths": []}


def test_a_locked_path_is_reported_with_its_reason_age_and_text(client, home):
    create_lock(provider="acme-testpath", lock_class="billing", reason="monthly spend limit",
                component="agent-loop")
    body = client.get("/api/provider-locks").json()
    assert [path["path"] for path in body["locked_paths"]] == ["acme-testpath"]
    locked = body["locked_paths"][0]
    assert locked["class"] == "billing" and locked["reason"] == "monthly spend limit"
    assert locked["component"] == "agent-loop" and locked["age"]
    assert "acme-testpath: billing — monthly spend limit" in locked["text"]


def test_reopen_clears_the_lock_and_returns_what_is_left(client, home):
    create_lock(provider="acme-testpath", lock_class="billing", reason="monthly spend limit")
    create_lock(provider="acme-second", lock_class="rate_limit", reason="rate limited until reset",
                ttl_seconds=900)
    response = client.post("/api/provider-locks/reopen", json={"path": "acme-testpath"})
    assert response.status_code == 200
    assert response.json()["reopened"] == "acme-testpath"
    assert [path["path"] for path in response.json()["locked_paths"]] == ["acme-second"]
    assert [lock.path for lock in list_locks()] == ["acme-second"]


def test_reopening_a_free_path_is_a_404_not_a_surprise(client, home):
    response = client.post("/api/provider-locks/reopen", json={"path": "acme-testpath"})
    assert response.status_code == 404
    assert "acme-testpath" in response.json()["detail"]


def test_an_ambiguous_name_is_a_409_and_touches_nothing(client, home):
    create_lock(provider="anthropic", auth="oauth", lock_class="billing", reason="no credits")
    create_lock(provider="anthropic", auth="api_key", lock_class="billing", reason="no credits")
    response = client.post("/api/provider-locks/reopen", json={"path": "anthropic"})
    assert response.status_code == 409
    assert len(list_locks()) == 2
    assert clear_lock("anthropic-oauth") is True  # the operator can still name one exactly
