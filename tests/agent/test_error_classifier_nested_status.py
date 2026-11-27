"""A 429 wrapped in a 502 is still a 429 — unwrap before classifying (#91091, #80553).

claude-sub relays its upstream's refusal inside an HTTP 502 envelope. Classifying the envelope
made a hard monthly quota read as a transient server error, so every session retried the same
dead account for an hour (2026-11-20). The nested status is the real verdict; the envelope only
says a relay carried it.
"""

import httpx
import openai
import pytest

from agent.error_classifier import FailoverReason, classify_api_error
from agent.provider_locks import ProviderLock, ProviderPathLocked

RELAY = "https://claude-sub.example.test/v1"
MODEL = "claude-opus-5-5"


def _error(status, body):
    request = httpx.Request("POST", f"{RELAY}/chat/completions")
    response = httpx.Response(status, json=body, request=request)
    message = (body.get("error") or {}).get("message") if isinstance(body.get("error"), dict) else None
    message = message or body.get("message") or "refused"
    if status == 429:
        return openai.RateLimitError(message, response=response, body=body)
    return openai.APIStatusError(message, response=response, body=body)


def _classify(error):
    return classify_api_error(error, provider="claude-sub", model=MODEL, base_url=RELAY)


def _wrapped(status, nested_status, message, **extra_error_fields):
    error = {"message": message, "code": nested_status}
    error.update(extra_error_fields)
    return _error(status, {"error": error})


# (id, nested status, message, expected reason)
UNWRAP_CASES = [
    # The 2026-11-20 failure: an exhausted monthly cap behind a 502 is billing, never a retry.
    ("monthly-usage-limit", 429, "You have reached your monthly usage limit.", FailoverReason.billing),
    ("monthly-spend-limit", 429, "Monthly spend limit reached for this account.", FailoverReason.billing),
    ("credits-exhausted", 429, "Insufficient credits: credits have been exhausted.", FailoverReason.billing),
    # A throttled 429 keeps its own class after the unwrap: backoff, not a closed path.
    ("throttled", 429, "Rate limit exceeded, please retry after 30 seconds.", FailoverReason.rate_limit),
    ("payment-required", 402, "Your account is out of funds.", FailoverReason.billing),
    ("key-refused", 401, "Invalid API key", FailoverReason.auth),
]


@pytest.mark.parametrize("case_id,nested_status,message,expected", UNWRAP_CASES,
                         ids=[c[0] for c in UNWRAP_CASES])
def test_nested_status_is_the_verdict_the_relay_carried(case_id, nested_status, message, expected):
    classified = _classify(_wrapped(502, nested_status, message))
    assert classified.reason is expected, f"{case_id}: {classified.reason} / {classified.message}"


def test_the_envelope_status_is_kept_for_display():
    classified = _classify(_wrapped(502, 429, "You have reached your monthly usage limit."))
    # Reading the record later must show what the relay answered as well as what it meant.
    assert classified.error_context.get("wrapped_status") == 502


def test_a_nested_status_never_invents_a_refusal_from_a_plain_bad_gateway():
    classified = _classify(_error(502, {"error": {"message": "Bad gateway: upstream connection reset"}}))
    assert classified.reason is FailoverReason.server_error


def test_a_nested_server_status_does_not_replace_the_envelope():
    # 5xx-in-5xx says nothing about an account; the outer status keeps its own retry story.
    classified = _classify(_wrapped(500, 503, "Upstream temporarily unavailable"))
    assert classified.status_code == 500
    assert classified.reason in {FailoverReason.server_error, FailoverReason.overloaded}


def test_an_unwrapped_429_is_unchanged():
    classified = _classify(_error(429, {"error": {"message": "Insufficient credits", "code": "insufficient_credits"}}))
    assert classified.reason is FailoverReason.billing


def test_a_502_with_no_nested_status_keeps_the_gateway_verdict():
    # v1 unwraps statuses, not prose: billing wording inside a 5xx envelope is deliberately NOT
    # re-read here, so a gateway page that mentions someone's quota cannot close a healthy path.
    classified = _classify(_error(502, {"error": {"message": "upstream rejected: insufficient_quota"}}))
    assert classified.reason is FailoverReason.server_error


# ── a lock reads as the unavailability it is, so the fallback chain walks on ──

def test_a_locked_path_classifies_as_provider_unavailability_that_falls_back():
    lock = ProviderLock(path="claude-sub", lock_class="billing", reason="monthly spend limit",
                        source="", locked_at=1_700_000_000.0, component="agent-loop", expires_at=None)
    classified = _classify(ProviderPathLocked(lock))
    assert classified.reason is FailoverReason.provider_path_locked
    # Never retried, always handed to the next rung: this is what stops the retry storm.
    assert classified.retryable is False
    assert classified.should_fallback is True
    assert classified.should_rotate_credential is False
    assert classified.error_context["locked_path"] == "claude-sub"
