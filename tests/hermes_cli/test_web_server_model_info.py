"""Payload shape of the gated ``GET /api/model/info`` route.

Moved out of ``test_web_server.py`` with the route: ``/api/model/info`` answers
with this host's live model and provider read out of ``load_config()``, so it is
no longer on the dashboard's public allowlist
(``hermes_cli/dashboard_auth/public_paths.py``) and a handler test needs the
loopback session token to reach it at all. The refusal — both gates, and the
``?profile=`` name oracle — is pinned in ``test_dashboard_auth_middleware.py``;
what stays here is what a credential buys.
"""

from unittest.mock import patch

import pytest

import hermes_cli.config as _cfg_mod


class TestModelInfoEndpoint:
    """Tests for GET /api/model/info endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_model_info_with_dict_config(self, monkeypatch):

        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {
            "model": {
                "default": "anthropic/claude-opus-4.6",
                "provider": "openrouter",
                "context_length": 100000,
            }
        })

        with patch("agent.model_metadata.get_model_context_length", return_value=200000):
            resp = self.client.get("/api/model/info")

        data = resp.json()
        assert data["model"] == "anthropic/claude-opus-4.6"
        assert data["provider"] == "openrouter"
        assert data["auto_context_length"] == 200000
        assert data["config_context_length"] == 100000
        assert data["effective_context_length"] == 100000  # override wins

    def test_model_info_graceful_on_metadata_error(self, monkeypatch):
        """Endpoint should return zeros on import/resolution errors, not 500."""

        monkeypatch.setattr(_cfg_mod, "load_config", lambda: {
            "model": "some/obscure-model"
        })

        with patch("agent.model_metadata.get_model_context_length", side_effect=Exception("boom")):
            resp = self.client.get("/api/model/info")

        assert resp.status_code == 200
        data = resp.json()
        assert data["auto_context_length"] == 0
