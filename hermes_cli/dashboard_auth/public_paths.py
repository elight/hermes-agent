"""Shared allowlist of ``/api/*`` paths that bypass dashboard auth. Imported by BOTH gates —
``web_server.auth_middleware`` (loopback / ``--insecure``) and
``dashboard_auth.middleware.gated_auth_middleware`` (OAuth cookie) — so the lists cannot drift
again (a drift once 401'd ``/api/status`` and broke the portal's cookie-less liveness probe).
Keep minimal: every entry must be safe for external uptime probes, the pre-login SPA, and anyone
who ``curl``s the hostname; otherwise gate it and bootstrap after login."""
from __future__ import annotations

PUBLIC_API_PATHS: frozenset[str] = frozenset({
    # Minimal process liveness probe for desktop/backend boot handshakes; avoids
    # gateway config, platform discovery, MCP setup and cold plugin imports. The
    # boundary is the handler itself: ``get_health`` takes no argument and reads
    # no config, profile or gateway state — it answers five fixed keys
    # (``ok``/``version``/``displayVersion``/``commit``/``auth_required``), i.e.
    # build identity, which is what makes the route exist at all (Desktop
    # refuses to attach to a backend whose ``commit`` is not its checkout).
    "/api/health",
    # Portal wildcard liveness probe (``docs/agent-dashboard-public-url-contract.md``,
    # NAS side). The only entry whose body is assembled per request, so its
    # boundary is inside the handler, not here: ``_status_caller_verified``
    # decides, and a caller it cannot verify on a gated bind gets one named key
    # set — ``_UNVERIFIED_STATUS_KEYS`` (``web_routers/status.py``): liveness
    # (``version``, ``release_date``, ``gateway_running``, ``gateway_state``,
    # ``overall``, ``components``) plus the sign-in shape (``auth_required``,
    # ``auth_providers``, ``auth_flows``, ``peer_access``), and on the surfaces
    # Hermes Cloud probes also ``_HOSTED_PROBE_STATUS_KEYS`` (``profiles``,
    # ``gateway_mode``, ``nous_session_valid``, ``gateway_busy``,
    # ``gateway_drainable``, and the code-only ``gateway_platforms`` with its
    # ``<profile>:<platform>`` keys folded onto bare platform names, plus
    # ``gateway_exit_reason``). Nothing else: no install id, no profile-to-platform
    # pairing, no host sizing, no session counts, no paths, no credential. The same
    # handler runs ``redact_status_for_unverified``
    # (``web_routers/status_public.py:45``) for any unverified caller — it bounds
    # adapter free-text to lifecycle enums and source-controlled codes on the
    # loopback bind, where the body is not narrowed — and ignores an unverified
    # caller's ``?profile=`` on every gated surface, because the scope lookup's
    # 404 for an unknown name disclosed which profiles exist.
    "/api/status",
    # The static ``DEFAULT_CONFIG`` snapshot the SPA's Config page seeds from:
    # identical bytes on every install, so a pre-login read leaks nothing. The
    # boundary is that the handler returns the constant and takes no argument
    # (``web_routers/config_env.py:96-98``), which also makes ``?profile=``
    # inert here — the one entry with no profile oracle to protect.
    # ``/api/config/schema`` is NOT here — it is built per request from the
    # requested profile (discovered plugin provider names, the user's custom
    # provider blocks) and belonged on this list only while its handler
    # returned a fixed constant.
    "/api/config/defaults",
    # NOT here either: ``/api/model/info``, ``/api/dashboard/themes`` and
    # ``/api/dashboard/plugins`` were listed beside these from the day the list
    # was written, against payloads that were already per-install — the live
    # model + provider (with ``?profile=`` turning it into a per-profile reader
    # whose context-length probe dials the operator's endpoint carrying that
    # profile's key), every operator-authored theme's definition, and the
    # installed-plugin inventory. Their boundary is the sign-in gate: every
    # consumer is post-login, and the pre-login 401 is the SPA's own sign-in
    # trigger (the gate's ``login_url`` envelope + ``web/src/lib/api.ts:222``),
    # not a broken shell.
    # Chronos managed-cron fire webhook (NAS -> agent). NOT cookie-gated: it
    # carries its own short-lived NAS-minted JWT (purpose=cron_fire), which the
    # handler verifies — the JWT, not this allowlist, is the security boundary.
    "/api/cron/fire"})
