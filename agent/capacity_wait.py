"""Cron-only capacity wait for a local engine's "at capacity" 429.

A single-slot local engine (gpud, llmbox ADR-230/231) answers 429 ``engine_at_capacity``
with ``Retry-After: N`` and "retry the same model in N seconds" after holding the door for
up to 40s. The classifier reads that wording as ``FailoverReason.overloaded``: the request is
fine, the slot is busy. For an attended session that verdict already backs off briefly, but a
cron job colliding with a long run (the Messenger draft holds the slot ~25 min) burns its
whole ``agent.api_max_retries`` ladder in ~2 minutes and fails.

In cron context only, when an overloaded failure carries a parseable provider retry hint and
the provider is a local engine, sleep the hint plus jitter (0-20%, so waiting jobs across
profiles do not all knock at the same second) and retry the SAME model — without counting
toward ``api_max_retries`` and without touching the fallback ladder, credential rotation or
the local-model bench/cooldown. The wait is bounded by a per-run wall-clock budget
(``cron.capacity_wait_seconds``, default 2400s; the cron scheduler caps it per job by the
schedule interval since waiting past the next fire is pointless — that fire coalesces as
"already running"). When the budget is spent we fall through to the normal retry path and the
job fails as today — never hang.

A 429 classified ``rate_limit`` (the caller firewall: "too many concurrent requests") is NOT
a capacity signal and never waits here; only ``FailoverReason.overloaded`` does.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Optional

from agent.error_classifier import FailoverReason
from agent.retry_utils import RETRY_AFTER_CAP_S, provider_retry_after_seconds

# Same logger as the retry loop so one log query shows the waits and the retries together.
logger = logging.getLogger("agent.conversation_loop")

CAPACITY_WAIT_CONFIG_KEY = "capacity_wait_seconds"
DEFAULT_CAPACITY_WAIT_SECONDS = 2400.0  # > the longest known slot hold (Messenger ~25 min)
JITTER_RATIO = 0.2

# Module-level indirection so tests control the clock and the jitter without touching the
# stdlib globally (the house pattern for the retry/backoff paths).
_monotonic = time.monotonic
_uniform = random.uniform


def capacity_wait_config_seconds(cfg: Any) -> float:
    """Resolve ``cron.capacity_wait_seconds`` (default 2400). ``0`` disables; a negative
    value disables; an unparseable value falls back to the default, never to 0 — a typo in
    config must not silently turn capacity waits back into ladder burns."""
    section = cfg.get("cron") if isinstance(cfg, dict) else None
    raw = section.get(CAPACITY_WAIT_CONFIG_KEY, DEFAULT_CAPACITY_WAIT_SECONDS) if isinstance(section, dict) else DEFAULT_CAPACITY_WAIT_SECONDS
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_WAIT_SECONDS


def _budget_seconds(agent: Any) -> float:
    """Per-run wall-clock wait budget. The cron scheduler attaches the job-capped value
    (min of config and the schedule interval); without it (any other cron-agent path) use
    the config value. Doubles that fabricate attributes must not masquerade as a budget."""
    budget = getattr(agent, "_cron_capacity_wait_budget_s", None)
    if isinstance(budget, (int, float)) and not isinstance(budget, bool):
        return max(0.0, float(budget))
    from hermes_cli.config import load_config
    return capacity_wait_config_seconds(load_config() or {})


def _job_label(agent: Any) -> str:
    """Job label for the log line (set by the cron scheduler); the session id is the
    fallback. Both reads must survive doubles that fabricate callables for any name."""
    for attr in ("_cron_capacity_wait_job_label", "session_id"):
        value = getattr(agent, attr, None)
        if isinstance(value, str) and value:
            return value
    return "?"


def _state(agent: Any, budget: float, now: float) -> dict:
    """One per-run state: the deadline is armed by the FIRST capacity wait (not run start —
    a job that waits 40 minutes total has spent its budget, however long the turn ran before
    the first 429)."""
    state = getattr(agent, "_capacity_wait_state", None)
    if isinstance(state, dict) and state.get("budget") == budget:
        return state
    state = {"deadline": now + budget, "budget": budget, "waits": 0, "gave_up": False}
    agent._capacity_wait_state = state
    return state


def capacity_wait_verdict(
    agent: Any, *, api_error: Exception, classified: Any, _retry: Any,
    messages: list[dict[str, Any]], conversation_history: Any, api_call_count: int,
) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """The caller (``handle_api_error``) takes the returned action, or proceeds with today's
    handling on ``(None, None)``.

    Gates (ALL must hold): cron context, ``FailoverReason.overloaded``, a parseable provider
    retry hint, a local-engine base_url, and a positive budget. Then sleep
    ``min(hint, RETRY_AFTER_CAP_S)`` + jitter clamped to the remaining budget through
    ``interruptible_backoff_sleep`` (interrupts honoured, activity touched every ~30s so the
    cron inactivity watchdog stays fed). Returns ``("continue", None)`` after a wait (retry
    the same request, ladder untouched), ``("break", None)`` when a steering redirect landed
    during the wait, ``("return", result)`` on interrupt, ``(None, None)`` when this is not a
    capacity wait or the budget is spent (give-up logged once at WARNING)."""
    if getattr(agent, "platform", None) != "cron":
        return None, None
    if classified is None or classified.reason is not FailoverReason.overloaded:
        return None, None
    hint = provider_retry_after_seconds(api_error)
    if hint is None:
        return None, None
    from agent.model_metadata import is_local_endpoint

    if not is_local_endpoint(str(getattr(agent, "base_url", "") or "")):
        return None, None
    budget = _budget_seconds(agent)
    if budget <= 0:
        return None, None

    now = _monotonic()
    state = _state(agent, budget, now)
    remaining = state["deadline"] - now
    model = str(getattr(agent, "model", "") or "?")
    if remaining <= 0:
        if not state["gave_up"]:
            state["gave_up"] = True
            logger.warning(
                "%sLocal-engine capacity wait exhausted: job=%s model=%s budget=%.0fs waits=%d "
                "— falling back to normal retries (the job fails as before if the slot is still busy)",
                agent.log_prefix, _job_label(agent), model, budget, state["waits"],
            )
        return None, None

    # Jitter FIRST, then cap: RETRY_AFTER_CAP_S and the remaining budget bound the sleep
    # itself, so a 20% spread can never push a wait past either cap.
    wait = min(hint * (1.0 + _uniform(0.0, JITTER_RATIO)), RETRY_AFTER_CAP_S, remaining)
    state["waits"] += 1
    logger.info(
        "%sLocal-engine capacity wait: job=%s model=%s hint=%.0fs sleeping=%.1fs "
        "elapsed=%.0fs budget=%.0fs — engine at capacity, holding the same model",
        agent.log_prefix, _job_label(agent), model, hint, wait,
        budget - remaining, budget,
    )
    from agent.turn_recovery import interruptible_backoff_sleep

    outcome = interruptible_backoff_sleep(
        agent, wait, _retry, messages=messages, conversation_history=conversation_history,
        api_call_count=api_call_count,
        abort_message="Interrupt detected during capacity wait, aborting.",
        interrupt_text=f"Operation interrupted: waiting for the {model} engine slot to free up.",
        activity_label=f"local-engine capacity wait ({budget - remaining:.0f}s/{budget:.0f}s elapsed)",
    )
    if outcome is not None:
        return "return", outcome
    if _retry is not None and getattr(_retry, "restart_with_redirected_messages", False):
        # A steer arrived during the wait: rebuild the turn from the correction instead of
        # replaying the stale request (same rule as the error-retry backoff).
        return "break", None
    return "continue", None
