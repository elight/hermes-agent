"""Cron-only capacity wait for a local engine's "at capacity" 429 (t_627ba2b1).

gpud's one-slot tenants answer 429 ``engine_at_capacity`` with ``Retry-After: 30`` after
holding the door up to 40s (llmbox ADR-230/231); the classifier maps the wording to
``FailoverReason.overloaded``. A cron job stuck behind a 25-minute run must sit the hint out
and retry the SAME model without burning ``agent.api_max_retries`` or touching the fallback
ladder — bounded by a per-run wall-clock budget (``cron.capacity_wait_seconds``, default
2400s, capped per job by the schedule interval on the cron side). Chat/gateway/TUI keep
today's behaviour byte for byte.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent.capacity_wait import (
    DEFAULT_CAPACITY_WAIT_SECONDS,
    capacity_wait_config_seconds,
    capacity_wait_verdict,
)
from agent.error_classifier import FailoverReason, classify_api_error
from agent.retry_utils import RETRY_AFTER_CAP_S
from agent.turn_retry_state import TurnRetryState

# The real gpud ADR-230/231 refusal shape: 429 + Retry-After: 30 + engine_at_capacity body.
_GPUD_MESSAGE = (
    "Error code: 429 - {'error': {'code': 'engine_at_capacity', 'message': "
    "\"The gpud tenant 'strata' is at capacity; retry the same model in 30 seconds.\"}}"
)
# The caller-firewall 429 (llmbox ADR-231): "too many concurrent requests" — a rate limit,
# NOT a capacity signal. It must never trigger a capacity wait.
_FIREWALL_MESSAGE = (
    "Error code: 429 - {'error': {'message': 'too many concurrent requests, please slow down'}}"
)


class _Engine429(Exception):
    """Stand-in for openai.RateLimitError from a 429 with headers + parsed body."""

    def __init__(self, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status_code = 429
        self.response = SimpleNamespace(headers=headers if headers is not None else {"retry-after": "30"})
        self.body = None


def _gpud_error() -> _Engine429:
    return _Engine429(_GPUD_MESSAGE)


def _classify(err, base_url="http://192.168.1.97:8080/v1", provider="custom"):
    return classify_api_error(err, provider=provider, model="strata-9b", base_url=base_url)


class _Agent:
    """Liveness-safe double: explicit fields the capacity wait reads, no-op for the rest.

    ``__getattr__`` fabricates callables for unknown names, so every value the wait reads
    must be set explicitly (a fabricated lambda must never masquerade as a budget/label).
    """

    log_prefix = ""
    verbose = False
    provider = "custom"
    model = "strata-9b"
    base_url = "http://192.168.1.97:8080/v1"  # LAN: gpud's real shape (is_local_endpoint True)
    platform = "cron"
    _interrupt_requested = False

    def __init__(self, *, platform="cron", base_url=None, budget=600.0, job_label="llmbox-pr-patrol (job_pat1)"):
        self.platform = platform
        if base_url is not None:
            self.base_url = base_url
        self.touches = []
        if budget is not None:
            self._cron_capacity_wait_budget_s = budget
        if job_label is not None:
            self._cron_capacity_wait_job_label = job_label

    def _touch_activity(self, desc, **kwargs):
        self.touches.append(desc)

    def _recover_with_credential_pool(self, **kwargs):
        return False, False

    def _adopt_nous_key_before_expiry(self):
        return None

    def clear_interrupt(self, **kwargs):
        return False

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.fixture
def sleeps(monkeypatch):
    """Capture every interruptible_backoff_sleep call the wait makes (args + return)."""
    calls = []

    def fake_sleep(agent, wait_time, _retry, **kwargs):
        calls.append({"wait": wait_time, "label": kwargs.get("activity_label")})

    monkeypatch.setattr("agent.turn_recovery.interruptible_backoff_sleep", fake_sleep)
    return calls


@pytest.fixture
def no_jitter(monkeypatch):
    monkeypatch.setattr("agent.capacity_wait._uniform", lambda lo, hi: 0.0)


@pytest.fixture
def capacity_wait_config():
    """Write cron.capacity_wait_seconds=45 into the test-isolated HERMES_HOME/config.yaml
    (the autouse home isolation gives us a temp home; the real load_config reads it)."""
    import os
    from pathlib import Path
    home = Path(os.environ["HERMES_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text("cron:\n  capacity_wait_seconds: 45\n", encoding="utf-8")
    return home


def _verdict(agent, err, *, classified=None, _retry=None):
    return capacity_wait_verdict(
        agent, api_error=err, classified=classified, _retry=_retry or TurnRetryState(),
        messages=[], conversation_history=None, api_call_count=3,
    )


# ── gate: only a cron + overloaded + hinted + local-engine error waits ──────────────

def test_gpud_at_capacity_refusal_classifies_as_overloaded():
    """The fixture matches the real wire contract: ADR-230 wording → overloaded, retryable,
    no eager fallback flag; the firewall wording → rate_limit (excluded from capacity waits)."""
    c = _classify(_gpud_error())
    assert c.reason is FailoverReason.overloaded and c.retryable
    assert classify_api_error(_Engine429(_FIREWALL_MESSAGE), provider="custom",
                              model="strata-9b").reason is FailoverReason.rate_limit


def test_cron_capacity_429_waits_the_jittered_hint_and_retries(no_jitter, sleeps):
    agent = _Agent()
    err = _gpud_error()
    action, result = _verdict(agent, err, classified=_classify(err))
    assert (action, result) == ("continue", None)
    assert len(sleeps) == 1 and sleeps[0]["wait"] == pytest.approx(30.0)


def test_jitter_spreads_waiters_up_to_twenty_percent(monkeypatch, sleeps):
    monkeypatch.setattr("agent.capacity_wait._uniform", lambda lo, hi: hi)
    agent = _Agent()
    err = _gpud_error()
    action, _ = _verdict(agent, err, classified=_classify(err))
    assert action == "continue"
    assert sleeps[0]["wait"] == pytest.approx(36.0)  # 30 * (1 + 0.2), the spread ceiling


def test_chat_platform_takes_todays_path_with_no_capacity_wait(sleeps):
    agent = _Agent(platform="telegram")
    err = _gpud_error()
    assert _verdict(agent, err, classified=_classify(err)) == (None, None)
    assert sleeps == []


def test_rate_limit_429_is_not_a_capacity_wait(sleeps):
    """The caller-firewall 429 ("too many concurrent requests") is a rate limit, not a slot
    busy signal — waiting the same 30s blindly is the wrong medicine (hard requirement 3)."""
    agent = _Agent()
    err = _Engine429(_FIREWALL_MESSAGE)
    assert _verdict(agent, err, classified=_classify(err)) == (None, None)
    assert sleeps == []


def test_remote_engine_capacity_429_is_not_a_capacity_wait(sleeps):
    agent = _Agent(base_url="https://openrouter.ai/api/v1")
    err = _gpud_error()
    assert _verdict(agent, err, classified=_classify(err, base_url=agent.base_url)) == (None, None)
    assert sleeps == []


def test_overloaded_without_retry_hint_is_not_a_capacity_wait(sleeps):
    agent = _Agent()
    err = _Engine429(_GPUD_MESSAGE, headers={})  # no Retry-After header, no body field
    assert _verdict(agent, err, classified=_classify(err)) == (None, None)
    assert sleeps == []


def test_zero_budget_disables_capacity_wait(sleeps):
    agent = _Agent(budget=0)
    err = _gpud_error()
    assert _verdict(agent, err, classified=_classify(err)) == (None, None)
    assert sleeps == []


# ── budget arithmetic: per-attempt caps and the give-up ────────────────────────────

def test_hint_capped_by_retry_after_cap(monkeypatch, sleeps):
    monkeypatch.setattr("agent.capacity_wait._uniform", lambda lo, hi: hi)
    agent = _Agent(budget=10_000.0)
    err = _Engine429(_GPUD_MESSAGE, headers={"retry-after": "900"})
    action, _ = _verdict(agent, err, classified=_classify(err))
    assert action == "continue"
    assert sleeps[0]["wait"] == pytest.approx(RETRY_AFTER_CAP_S)  # 600, not 900 * 1.2


def test_hint_capped_by_remaining_budget(monkeypatch, sleeps):
    clock = {"now": 1000.0}
    monkeypatch.setattr("agent.capacity_wait._monotonic", lambda: clock["now"])
    monkeypatch.setattr("agent.capacity_wait._uniform", lambda lo, hi: hi)
    agent = _Agent(budget=100.0)
    err = _gpud_error()
    assert _verdict(agent, err, classified=_classify(err))[0] == "continue"
    clock["now"] += 80.0  # 20s of budget left; a 30s (+jitter) hint would overshoot
    assert _verdict(agent, err, classified=_classify(err))[0] == "continue"
    assert sleeps[-1]["wait"] == pytest.approx(20.0)


def test_budget_exhaustion_falls_through_and_warns_once(monkeypatch, caplog, sleeps, no_jitter):
    clock = {"now": 1000.0}
    monkeypatch.setattr("agent.capacity_wait._monotonic", lambda: clock["now"])
    agent = _Agent(budget=100.0)
    err = _gpud_error()
    assert _verdict(agent, err, classified=_classify(err))[0] == "continue"
    clock["now"] += 101.0
    with caplog.at_level(logging.WARNING, logger="agent.conversation_loop"):
        assert _verdict(agent, err, classified=_classify(err)) == (None, None)
        assert _verdict(agent, err, classified=_classify(err)) == (None, None)
    give_ups = [r for r in caplog.records if "capacity" in r.getMessage().lower()
                and r.levelno == logging.WARNING]
    assert len(give_ups) == 1  # one WARNING per run, not per post-give-up attempt
    assert sleeps[-1]["wait"] == pytest.approx(30.0)  # no sleep was taken after exhaustion


def test_each_wait_logged_at_info_with_job_model_hint_and_budget(caplog):
    agent = _Agent(budget=2400.0)
    err = _gpud_error()
    with caplog.at_level(logging.INFO, logger="agent.conversation_loop"):
        assert _verdict(agent, err, classified=_classify(err))[0] == "continue"
    line = next(r.getMessage() for r in caplog.records
                if r.levelno == logging.INFO and "capacity" in r.getMessage().lower())
    for needle in ("llmbox-pr-patrol", "strata-9b", "30", "2400"):
        assert needle in line


# ── config ─────────────────────────────────────────────────────────────────────────

def test_config_key_default_zero_and_garbage():
    assert capacity_wait_config_seconds({}) == DEFAULT_CAPACITY_WAIT_SECONDS
    assert capacity_wait_config_seconds({"cron": {"capacity_wait_seconds": 900}}) == 900.0
    assert capacity_wait_config_seconds({"cron": {"capacity_wait_seconds": 0}}) == 0.0
    assert capacity_wait_config_seconds({"cron": {"capacity_wait_seconds": -5}}) == 0.0
    assert capacity_wait_config_seconds({"cron": {"capacity_wait_seconds": "wide"}}) == DEFAULT_CAPACITY_WAIT_SECONDS


def test_budget_falls_back_to_config_when_cron_did_not_attach(sleeps, no_jitter, capacity_wait_config):
    """The config key is read through the real ``load_config`` loader: a config.yaml with
    ``cron.capacity_wait_seconds`` set must change the wait budget of a cron agent the
    scheduler never attached a budget to (hermes_cli rule: every DEFAULT_CONFIG key needs a
    runtime reader, proven through the surface's loader)."""
    agent = _Agent(budget=None, job_label=None)  # no _cron_capacity_wait_budget_s attribute
    agent._cron_capacity_wait_job_label = None  # double-fabrication guard: label must not be a lambda
    err = _gpud_error()
    assert _verdict(agent, err, classified=_classify(err))[0] == "continue"
    assert sleeps[0]["wait"] == pytest.approx(30.0)  # hint fits inside the 45s config budget


def test_config_below_the_hint_caps_the_wait(sleeps, no_jitter, capacity_wait_config):
    """Budget semantics through the loader: with 45s of budget, a 60s hint is capped to 45s."""
    agent = _Agent(budget=None, job_label=None)
    err = _Engine429(_GPUD_MESSAGE, headers={"retry-after": "60"})
    assert _verdict(agent, err, classified=_classify(err))[0] == "continue"
    assert sleeps[0]["wait"] == pytest.approx(45.0)


# ── integration through handle_api_error: no ladder burn, unchanged elsewhere ──────

def _run_handle(agent, err, monkeypatch, waits):
    """Push the gpud 429 through handle_api_error with the neighbouring phases stubbed.

    The route/settle stubs pass loop locals through unchanged (real route only rewrites
    them on its own recovery actions), so the ladder arithmetic observed here is the real
    ``handle_api_error`` arithmetic. ``waits`` captures every interruptible-backoff seconds
    value actually slept."""
    from agent.turn_api_error import handle_api_error

    settled = []
    monkeypatch.setattr("agent.turn_api_error.recover_before_classification", lambda *a, **k: (False, ""))
    monkeypatch.setattr("agent.turn_api_error.recover_after_classification", lambda *a, **k: (False, False))

    def fake_route(agent, api_error, classified, _retry, **k):
        return SimpleNamespace(
            action="fallthrough", result=None, status_code=429, messages=k["messages"],
            active_system_prompt=k["active_system_prompt"], conversation_history=k["conversation_history"],
            retry_count=k["retry_count"], max_retries=k["max_retries"],
            compression_attempts=k["compression_attempts"], provider_overflow_recovery_pending=False,
            is_rate_limited=False, wrapped_output_cap_budget=None, is_zai_coding_overload=False)

    monkeypatch.setattr("agent.turn_api_error.route_classified_error", fake_route)
    monkeypatch.setattr("agent.turn_api_error.recover_from_overflow", lambda *a, **k: SimpleNamespace(
        action="fallthrough", result=None, messages=k["messages"],
        active_system_prompt=k["active_system_prompt"], conversation_history=k["conversation_history"],
        approx_tokens=k["approx_tokens"], compression_attempts=k["compression_attempts"],
        is_context_length_error=False, provider_overflow_recovery_pending=False))
    monkeypatch.setattr("agent.turn_api_error.settle_unrecovered_error", lambda agent, **k: (
        settled.append(k["retry_count"]) or SimpleNamespace(
            action="fallthrough", retry_count=k["retry_count"], compression_attempts=0,
            active_system_prompt="", result=None)))
    monkeypatch.setattr("tools.interpreter_shutdown.interpreter_shutting_down", lambda *a: False)
    monkeypatch.setattr("agent.turn_recovery.interruptible_backoff_sleep",
                        lambda agent, wait, _retry, **k: waits.append(wait) or None)
    verdict = handle_api_error(
        agent, api_error=err, _retry=TurnRetryState(), thinking_spinner=None, messages=[],
        api_messages=[], api_kwargs={}, system_message="", active_system_prompt="",
        conversation_history=None, approx_tokens=10, retry_count=1, max_retries=3,
        compression_attempts=0, max_compression_attempts=2, api_call_count=4,
        api_request_id="r1", api_start_time=0.0, effective_task_id="t1", turn_id="u1")
    return verdict, settled


def test_handle_api_error_capacity_wait_continues_without_burning_the_ladder(no_jitter, monkeypatch):
    waits = []
    agent = _Agent()
    verdict, settled = _run_handle(agent, _gpud_error(), monkeypatch, waits)
    assert verdict.action == "continue"
    assert verdict.retry_count == 1   # the incoming count survived: the ladder was NOT burned
    assert waits == [pytest.approx(30.0)]  # one capacity sleep, the hint
    assert settled == []              # never reached the retry/exhaustion machinery


def test_handle_api_error_chat_platform_unchanged(monkeypatch):
    waits = []
    agent = _Agent(platform="telegram")
    verdict, settled = _run_handle(agent, _gpud_error(), monkeypatch, waits)
    assert settled == [2]             # today's path: retry_count incremented toward max_retries
    assert waits == []                # no capacity sleep anywhere
    assert verdict.action == "fallthrough"
