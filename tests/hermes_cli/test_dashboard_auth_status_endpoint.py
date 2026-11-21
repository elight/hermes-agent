"""Phase 7 — /api/status exposes auth-gate state + AuthWidget integration.

The dashboard's status endpoint now reports ``auth_required`` and
``auth_providers`` so the AuthWidget + StatusPage can render the
correct "gated / loopback" badge without a separate round trip. This
test asserts both shapes (gated and loopback).

The AuthWidget itself is .tsx — no Python test here. The widget's
behaviour (renders nothing on 401, shows truncated user_id, etc.) is
documented in AuthWidget.tsx; covered manually via the Phase 4.2
smoke test against staging Portal.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_client():
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def loopback_client():
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8080
    web_server.app.state.auth_required = False
    client = TestClient(web_server.app, base_url="http://127.0.0.1:8080")
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def test_status_reports_auth_required_in_gated_mode(gated_client):
    # No ``_login()`` call — ``/api/status`` is in the shared
    # ``PUBLIC_API_PATHS`` allowlist precisely so external probes (and
    # the SPA's pre-login bootstrap) can read the gate's shape without
    # a cookie. Hit it cold.
    r = gated_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is True
    assert body["auth_providers"] == ["stub"]



# Host-local detail (absolute paths, PID, internal gateway URL) is deployment
# recon a liveness probe never needs. ``/api/status`` bypasses dashboard auth
# (it is in ``PUBLIC_API_PATHS``), so on a network-exposed bind it must not
# leak that detail to anonymous callers.
_HOST_DETAIL_FIELDS = frozenset({
    "hermes_home", "config_path", "env_path", "gateway_pid",
    "gateway_health_url",
})


def test_status_withholds_host_detail_in_gated_mode(gated_client):
    """On a gated (non-loopback) bind, the public ``/api/status`` probe must
    expose only the liveness + auth-gate shape — never absolute host paths,
    the gateway PID, or the internal gateway health URL. The endpoint
    bypasses dashboard auth, so anyone who can reach the host hits it cold."""
    r = gated_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    # Liveness / auth-gate shape stays public.
    for key in ("version", "gateway_state", "auth_required", "auth_providers"):
        assert key in body, f"liveness field {key!r} must stay public"
    # Deployment recon must be withheld from the anonymous public probe.
    leaked = _HOST_DETAIL_FIELDS & set(body.keys())
    assert not leaked, f"/api/status leaked host detail under the gate: {leaked}"


# ---------------------------------------------------------------------------
# The narrowed body an UNVERIFIED caller gets, on every gated surface.
#
# /api/status stays public (it is the portal's and Desktop's pre-credential
# probe), so what protects an install is the key set, not the route. Two sets
# below, each entry justified by the credential-free reader named in
# ``hermes_cli/web_routers/status.py``'s comments and in the card's PR body:
# the per-field table. Anything without such a reader is withheld until the
# caller carries a credential the auth gate would accept.
# ---------------------------------------------------------------------------

#: Liveness + the sign-in shape: what Desktop's credential-free probes and any
#: uptime check read. This is the body the gateway's Webapp has answered a
#: signed-out caller with since fd9ff2a0cd, so it is the accepted floor.
_LIVENESS_KEYS = frozenset({
    "version", "release_date", "gateway_running", "gateway_state", "overall", "components",
    "auth_required", "auth_providers", "auth_flows", "peer_access",
})

#: Added on every gated surface EXCEPT the Webapp: Hermes Cloud / NAS probes the
#: standalone dashboard's listener without a cookie (#60585) and reads the profile
#: list, the Nous-session enum it re-mints on, the busy/drainable pair it gates a
#: lifecycle action on, and the code-only platform verdict #90700 ruled public.
_HOSTED_KEYS = frozenset({
    "profiles", "gateway_mode", "nous_session_valid", "gateway_busy", "gateway_drainable",
    "gateway_platforms", "gateway_exit_reason",
})

#: Every one of these reaches an anonymous caller on a gated bind today and has
#: no credential-free reader anywhere on this branch: per-install identity, the
#: served/parked topology the dashboard's own controls read after sign-in, host
#: sizing, and the session/agent counts.
_WITHHELD_UNTIL_VERIFIED = frozenset({
    "install_id", "parked_profiles", "gateway_shared_with",
    "memory", "disk", "fts_rebuild", "active_sessions", "active_agents",
    "shared_profile_warning", "isolated_profile", "multiplex_standalone_reason",
    "config_version", "latest_config_version", "can_update_hermes", "import_keep_host",
    "restart_drain_timeout", "gateway_updated_at", "gateway_heartbeat_stale_s",
}) | _HOST_DETAIL_FIELDS

_STUB_TOPOLOGY = {
    "profiles": ["default", "coder"], "parked_profiles": ["research"],
    "gateway_mode": "multiplex", "gateways": [{"profile": "default", "port": 8642}],
    "multiplex_standalone_reason": "another gateway owns the host",
}


@pytest.fixture
def gated_surface(request):
    """One wildcard listener behind the sign-in gate, pinned to one surface.

    ``ui_surface`` is set deliberately rather than left to
    ``web_server_surface.surface()``'s fallback: the whole point is that the
    narrowing no longer depends on which surface string a listener reports, so
    a test that silently read as another surface would prove nothing.
    """
    surface_name = request.param
    assert surface_name in _GATED_SURFACES, f"unknown surface in the parametrization: {surface_name}"
    topology = dict(_STUB_TOPOLOGY)
    clear_providers()
    register_provider(StubAuthProvider())
    saved = {}
    for key, value in (("bound_host", "fly-app.fly.dev"), ("bound_port", 443),
                       ("auth_required", True), ("ui_surface", surface_name),
                       ("trusted_public_hosts", frozenset())):
        saved[key] = getattr(web_server.app.state, key, None)
        setattr(web_server.app.state, key, value)
    from hermes_cli import web_server_gateway
    real_topology = web_server_gateway._collect_profile_gateway_topology_cached
    web_server_gateway._collect_profile_gateway_topology_cached = lambda: topology
    client = TestClient(web_server.app, base_url="https://fly-app.fly.dev")
    yield surface_name, client
    web_server_gateway._collect_profile_gateway_topology_cached = real_topology
    clear_providers()
    for key, value in saved.items():
        setattr(web_server.app.state, key, value)


def _complete_stub_login(client) -> None:
    """Walk the stub OAuth round trip so ``client`` carries a session cookie."""
    start = client.get("/auth/login?provider=stub", follow_redirects=False)
    assert start.status_code == 302
    state = start.headers["location"].split("state=")[1]
    back = client.get(f"/auth/callback?code=stub_code&state={state}", follow_redirects=False)
    assert back.status_code == 302


#: Every surface the auth gate can engage on a wildcard bind. Expectations derive
#: from the surface name, so a new surface has to be listed here to be covered.
_GATED_SURFACES = ("dashboard", "gateway", "webapp")


def _expected_keys(surface_name: str) -> frozenset[str]:
    """The Webapp answers a signed-out caller with the liveness floor only
    (fd9ff2a0cd); the surfaces Hermes Cloud probes keep the hosted reads."""
    return _LIVENESS_KEYS if surface_name == "webapp" else _LIVENESS_KEYS | _HOSTED_KEYS


@pytest.mark.parametrize("gated_surface", _GATED_SURFACES, indirect=True)
def test_anonymous_gated_status_answers_only_the_justified_keys(gated_surface):
    """The anonymous body is the allowlist, exactly, on every gated surface.

    Key-set equality is the point: a field added to the payload later reaches an
    anonymous caller only if someone adds it here and names its reader.
    """
    surface_name, client = gated_surface
    body = client.get("/api/status").json()
    expected = _expected_keys(surface_name)
    assert set(body) <= expected, (
        f"{surface_name}: unjustified keys for an unverified caller: {set(body) - expected}")
    for key in expected:
        assert key in body, f"{surface_name}: {key!r} has a named pre-credential reader and must stay"
    leaked = _WITHHELD_UNTIL_VERIFIED & set(body)
    assert not leaked, f"{surface_name}: /api/status disclosed per-install detail anonymously: {leaked}"
    # ``isSignedOutStatus`` (apps/desktop/src/lib/signed-out-status.ts) classes a
    # trimmed body by the absence of BOTH of these, so a narrowed body must lack
    # both or the renderer reads it as a full body from an older backend.
    assert "active_sessions" not in body and "install_id" not in body


@pytest.mark.parametrize("gated_surface", _GATED_SURFACES, indirect=True)
def test_anonymous_status_hosted_consumer_fields_are_surface_scoped(gated_surface):
    """The hosted reads ride the surfaces Hermes Cloud probes, not the Webapp.

    fd9ff2a0cd withheld them on the Webapp; the portal probes the standalone
    dashboard, where #60585 put them. Widening either arm is a decision.
    """
    surface_name, client = gated_surface
    body = client.get("/api/status").json()
    hosted = surface_name != "webapp"
    for key in _HOSTED_KEYS:
        assert (key in body) is hosted, f"{key!r} visibility is wrong on the {surface_name} surface"
    if hosted:
        assert body["profiles"] == ["default", "coder"]
        assert body["gateway_mode"] == "multiplex"
        assert body["nous_session_valid"] in ("valid", "terminal", "unknown")
        assert isinstance(body["gateway_busy"], bool)


@pytest.mark.parametrize("gated_surface", _GATED_SURFACES, indirect=True)
def test_anonymous_profile_param_is_no_longer_a_profile_name_oracle(gated_surface):
    """``?profile=<bogus>`` answers 200 with no profile-scoped data on any gated
    surface. Before the fix the standalone dashboard ran the scope lookup for an
    unverified caller and its 404 named which profiles exist."""
    _, client = gated_surface
    bogus = "definitely-not-a-profile-name"
    unknown = client.get(f"/api/status?profile={bogus}")
    assert unknown.status_code == 200, (
        f"an unverified ?profile= must not answer {unknown.status_code}: that is a name oracle")
    assert "does not exist" not in unknown.text
    assert bogus not in unknown.text
    # An unknown name and a real one are indistinguishable to an anonymous caller.
    body = client.get("/api/status?profile=coder").json()
    for key in ("gateway_shared_with", "isolated_profile", "parked_profiles"):
        assert key not in body, f"{key!r} is profile-scoped and must wait for sign-in"


@pytest.mark.parametrize("gated_surface", _GATED_SURFACES, indirect=True)
def test_a_signed_in_caller_still_gets_the_full_body_on_every_surface(gated_surface):
    """Control for the narrowing: the gate's own session restores the withheld set."""
    _, client = gated_surface
    _complete_stub_login(client)
    body = client.get("/api/status").json()
    for key in ("profiles", "parked_profiles", "gateway_shared_with", "gateway_platforms",
                "active_sessions", "memory", "disk", "restart_drain_timeout"):
        assert key in body, f"verified caller lost {key!r}"
    assert body["parked_profiles"] == ["research"]
    # A verified caller keeps the profile oracle: a bogus name is a real mistake.
    assert client.get("/api/status?profile=definitely-not-a-profile-name").status_code == 404
