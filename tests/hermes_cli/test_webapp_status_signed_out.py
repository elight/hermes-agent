"""A gated listener answers an unverified ``/api/status`` caller from one named key set.

``/api/status`` is public (``PUBLIC_API_PATHS``): the native-app login probe and uptime checks read
it without a session. Every gated surface is reachable by anyone who can route to it, so an
unverified caller gets liveness and the sign-in shape only; the install id, the platform inventory
and the host figures wait for sign-in. The surfaces Hermes Cloud probes (everything but the
gateway's Webapp) additionally keep the few fields the portal and the NAS sweep read with no
credential (#60585).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import hermes_cli.web_server_gateway as _web_server_gateway
import plugins.dashboard_auth.basic as basic_plugin
from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from hermes_cli.dashboard_auth.routes import _reset_password_rate_limit

LAN = "http://192.168.50.220:9130"
IDENTITY = ("profiles", "parked_profiles", "gateway_shared_with", "install_id", "memory", "disk",
            "nous_session_valid", "active_sessions", "gateway_mode")
#: The subset of IDENTITY a hosted caller (Hermes Cloud / NAS) reads without a credential, so it
#: stays on the surfaces that listener probes and stays withheld on the Webapp (fd9ff2a0cd).
HOSTED_READS = ("profiles", "gateway_mode", "nous_session_valid")


@pytest.fixture(scope="module")
def password_hash():
    return basic_plugin.hash_password("hunter2")


@pytest.fixture(params=["dashboard", "webapp"])
def wildcard_gate(request, password_hash, monkeypatch):
    """An all-interfaces listener behind the bundled username/password sign-in: the standalone
    dashboard or the gateway's Webapp, with two profiles installed."""
    monkeypatch.delenv("HERMES_DASHBOARD_PUBLIC_URL", raising=False)
    for key, value in (("bound_host", "0.0.0.0"), ("bound_port", 9130), ("auth_required", True),
                       ("trusted_public_hosts", frozenset())):
        monkeypatch.setattr(web_server.app.state, key, value, raising=False)
    for key in ("ui_surface", "listener_public_url", "webapp_home"):
        if hasattr(web_server.app.state, key):
            monkeypatch.delattr(web_server.app.state, key)
    if request.param == "webapp":
        monkeypatch.setattr(web_server.app.state, "ui_surface", "webapp", raising=False)
        monkeypatch.setattr(web_server.app.state, "listener_public_url", "", raising=False)
    monkeypatch.setattr(_web_server_gateway, "_collect_profile_gateway_topology_cached", lambda: {
        "profiles": ["default", "coder"], "parked_profiles": [], "gateway_mode": "multiplex",
        "gateways": []})
    clear_providers()
    register_provider(basic_plugin.BasicAuthProvider(
        username="admin", password_hash=password_hash, secret=b"s" * 32))
    _reset_password_rate_limit()
    yield request.param
    clear_providers()


def test_signed_out_status_carries_no_profile_or_install_identity(wildcard_gate):
    client = TestClient(web_server.app, base_url=LAN)
    signed_out = client.get("/api/status")
    assert signed_out.status_code == 200
    body = signed_out.json()
    # What the native-app login probe and uptime checks read survives on every surface.
    assert body["auth_required"] is True and body["auth_providers"] == ["basic"]
    assert body["gateway_running"] in (True, False) and body["overall"] in ("ok", "degraded")
    assert body["version"]
    # Per-install identity and host sizing wait for sign-in on BOTH surfaces; the hosted
    # reads Hermes Cloud needs without a credential are the only difference between them.
    withheld = list(IDENTITY) if wildcard_gate == "webapp" else [
        key for key in IDENTITY if key not in HOSTED_READS]
    assert [key for key in withheld if key in body] == []
    if wildcard_gate == "dashboard":
        assert body["profiles"] == ["default", "coder"]  # Hermes Cloud's contract (#60585)
    # ``?profile=`` is no profile-name oracle on either surface: an unknown name answers 200,
    # and only a signed-in caller learns which names exist.
    assert client.get("/api/status?profile=nope").status_code == 200
    assert "parked_profiles" not in client.get("/api/status?profile=coder").json()

    login = client.post("/auth/password-login",
                        json={"provider": "basic", "username": "admin", "password": "hunter2"})
    assert login.status_code == 200, login.text
    signed_in = client.get("/api/status").json()
    assert signed_in["profiles"] == ["default", "coder"]
    assert signed_in["install_id"]
    assert "parked_profiles" in signed_in
    assert client.get("/api/status?profile=nope").status_code == 404


def test_a_credential_the_gate_would_refuse_reads_as_signed_out(wildcard_gate):
    client = TestClient(web_server.app, base_url=LAN)
    login = client.post("/auth/password-login",
                        json={"provider": "basic", "username": "admin", "password": "hunter2"})
    assert login.status_code == 200, login.text
    # A caller whose ONLY credential the gate refuses gets the narrowed body, on the
    # dashboard exactly as on the Webapp. (A client whose own jar still holds a live
    # session authenticates with that cookie whatever bearer it also sends — the gate's
    # rule, not this card's, so the two callers below carry no jar of their own.)
    forged_cookie = TestClient(web_server.app, base_url=LAN)
    for cookie in client.cookies.jar:
        value = (cookie.value or "") if "signin" in cookie.name else "forged"
        forged_cookie.cookies.set(cookie.name, value, domain=cookie.domain, path=cookie.path)
    responses = (("forged session cookie", forged_cookie.get("/api/status")),
                 ("forged bearer token", TestClient(web_server.app, base_url=LAN).get(
                     "/api/status", headers={"Authorization": "***"})))
    withheld = ("install_id", "parked_profiles", "gateway_shared_with", "active_sessions",
                "memory", "disk")
    for label, response in responses:
        body = response.json()
        for key in withheld:
            assert key not in body, f"{label}: {key!r} reached an unverified caller"
        assert body["auth_required"] is True
    if wildcard_gate == "webapp":
        for label, response in responses:
            assert "profiles" not in response.json(), f"{label}: {label} unlocked the profile list"
    # Control: the real cookie does authenticate, and the withheld set comes back with it.
    assert "install_id" in client.get("/api/status").json()
