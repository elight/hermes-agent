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
    # decides, and ``redact_status_for_unverified``
    # (``web_routers/status_public.py:45``) reduces an unverified caller's
    # free-text diagnostics to lifecycle enums and source-controlled codes,
    # while the ``if not auth["auth_required"]`` block keeps host paths, the
    # gateway PID and ``gateways[]`` off a gated bind. An unverified caller on a
    # gated bind still gets version, gateway state, session counts, the
    # auth-gate shape, ``install_id``, the profile names and host memory/disk
    # sizing — no credential, no session content, no path.
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
