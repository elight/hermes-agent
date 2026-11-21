"""What an unverified ``/api/status`` caller may see of gateway diagnostics (#90700).

``/api/status`` is public; the router calls :func:`redact_status_for_unverified` on the body for
any caller the auth gate would not accept on a gated route. Two projections happen there: each
platform entry is reduced to bounded codes, and the multiplexer's merged ``<profile>:<platform>``
keys are folded onto the bare platform name, because which profile runs which bot is per-profile
detail an anonymous caller does not get.
"""
from __future__ import annotations

import re
from typing import Any

from gateway.status import normalize_updated_at


# What an unverified caller may read of a platform entry: lifecycle enum, the source-controlled
# error code, the attention flag and timestamps. ``error_message`` is adapter exception text (endpoint
# URLs, response bodies, credential names) and any other key is whatever an adapter chose to write,
# so the anonymous projection is an allowlist, never a denylist (#90700).
#: Ordered worst-first: the members bound the ``state`` value and the order is the fold's
#: severity in :func:`_anonymous_platform_entries`.
_PLATFORM_SEVERITY = ("fatal", "retrying", "disconnected", "paused", "connecting",
                      "connected", "running", "ok", "disabled")
_PUBLIC_PLATFORM_STATES = frozenset(_PLATFORM_SEVERITY)
# Source-controlled codes (``telegram_polling_conflict``, ``loop_liveness_watchdog``) are snake_case
# identifiers; anything else (spaces, URLs, quotes, ``=``) is prose and does not pass.
_PUBLIC_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _anonymous_platform_entry(value: Any) -> dict[str, Any]:
    """Allowlisted, value-validated view of one platform entry for an unverified caller."""
    if not isinstance(value, dict):
        return {}
    entry: dict[str, Any] = {}
    state = value.get("state")
    if state is not None:
        entry["state"] = state if isinstance(state, str) and state in _PUBLIC_PLATFORM_STATES else "unknown"
    code = value.get("error_code")
    if isinstance(code, str) and _PUBLIC_CODE_RE.fullmatch(code):
        entry["error_code"] = code
    if isinstance(value.get("needs_attention"), bool):
        entry["needs_attention"] = value["needs_attention"]
    for key in ("retrying_since", "updated_at"):
        stamp = normalize_updated_at(value.get(key)) if isinstance(value.get(key), str) else None
        if stamp is not None:
            entry[key] = stamp
    return entry


def _anonymous_platform_entries(entries: dict[str, Any]) -> dict[str, Any]:
    """Bounded platform entries, keyed by the bare platform name.

    A served profile's entries arrive merged as ``<profile>:<platform>``. Which platforms this
    install runs is already public (``components.platforms`` counts them, and a hosted caller
    gets ``profiles``); the *pairing* — "coder runs Telegram" — is per-profile detail, so the
    anonymous view folds each key onto its platform. Worst state wins, so folding can never
    hide a ``fatal`` behind a healthy sibling, and ties break on the sorted key.
    """
    folded: dict[str, dict[str, Any]] = {}
    for key in sorted(entries):
        entry = _anonymous_platform_entry(entries[key])
        platform = key.rsplit(":", 1)[-1] if ":" in key else key
        held = folded.get(platform)
        if held is None or _platform_severity(entry) < _platform_severity(held):
            folded[platform] = entry
    return folded


def _platform_severity(entry: dict[str, Any]) -> int:
    """Index of the entry's state in :data:`_PLATFORM_SEVERITY`; unknown sorts last."""
    try:
        return _PLATFORM_SEVERITY.index(entry.get("state"))
    except ValueError:
        return len(_PLATFORM_SEVERITY)


def redact_status_for_unverified(status: dict[str, Any]) -> None:
    """Reduce the free-text diagnostics of a ``/api/status`` body to bounded codes in place.
    Full detail stays for a caller the auth gate would accept on a gated route."""
    from agent.monitoring.gateway_health import classify_exit_reason
    status["gateway_platforms"] = _anonymous_platform_entries(
        status.get("gateway_platforms") or {})
    reason = status.get("gateway_exit_reason")
    if reason is not None and not (isinstance(reason, str) and _PUBLIC_CODE_RE.fullmatch(reason)):
        status["gateway_exit_reason"] = classify_exit_reason(
            reason, state=status.get("gateway_state"), restart_requested=False)
    # Remediation prose naming the colliding profiles and the credential's env var.
    status["multiplex_standalone_reason"] = None
