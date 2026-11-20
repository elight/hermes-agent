"""Retryable pre-delivery stream failures log ONE WARNING, not a traceback (t_68927ab4).

The gateway journal carried ~410 ERROR-plus-~30-frame-traceback records per day for
``openai.APIError: strata is at capacity ... retry the same model in 30 seconds`` —
a retryable 503 that the conversation loop already retries and logs itself
("API call failed (attempt N/M)" + "Retrying API call in ...s"). The stream worker
must classify the failure at the log site: retryable -> single-line WARNING with no
``exc_info``; non-retryable -> keep ``logger.exception`` so real faults keep their
traceback.
"""
import logging
from types import SimpleNamespace

import httpx
import openai
import pytest

from agent import chat_completion_helpers as helpers

_CAPACITY_MSG = (
    "strata is at capacity: it is already running 1 jobs and it has 1 slots; "
    "retry the same model in 30 seconds"
)


class _RecordingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture()
def logged():
    """Records emitted on the module's own logger, independent of global handlers."""
    handler = _RecordingHandler()
    helpers.logger.addHandler(handler)
    try:
        yield handler
    finally:
        helpers.logger.removeHandler(handler)


def _capacity_error():
    request = httpx.Request("POST", "http://127.0.0.1:1/v1/chat/completions")
    return openai.APIError(_CAPACITY_MSG, request, body=None)


def _auth_error():
    response = httpx.Response(401, request=httpx.Request("POST", "http://127.0.0.1:1/v1"))
    return openai.AuthenticationError("invalid api key", response=response, body=None)


def _agent_ns():
    return SimpleNamespace(provider="custom", model="m", base_url="http://127.0.0.1:1/v1")


# ── unit: the log-site helper's level decision ─────────────────────────────

def test_retryable_capacity_logs_one_warning_without_traceback(logged):
    helpers._log_stream_failure_before_delivery(_agent_ns(), _capacity_error())
    assert len(logged.records) == 1
    record = logged.records[0]
    assert record.levelno == logging.WARNING, "a retried-by-design 503 must not reach ERROR"
    assert record.exc_info is None, "a retried-by-design 503 must not carry a traceback"
    message = record.getMessage()
    assert _CAPACITY_MSG in message
    assert "overloaded" in message, "the classification should say why it is retryable"
    assert "\n" not in message, "one journal record per record: the line stays single-line"


def test_non_retryable_keeps_the_exception_traceback(logged):
    helpers._log_stream_failure_before_delivery(_agent_ns(), _auth_error())
    errors = [r for r in logged.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None, "non-retryable faults must keep the traceback"


def test_classification_blindness_falls_back_to_the_traceback(logged, monkeypatch):
    """If classification itself explodes, fail closed toward the old behaviour:
    ERROR with exc_info, never a swallowed or under-logged real fault."""
    import agent.error_classifier as ec
    monkeypatch.setattr(ec, "classify_api_error", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    helpers._log_stream_failure_before_delivery(_agent_ns(), _capacity_error())
    errors = [r for r in logged.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None


# ── wiring: _handle_stream_error routes through the helper ──────────────────
#
# A bare ``_StreamingCall`` instance built via ``__new__`` instead of the real
# constructor: a full ``AIAgent`` init reads the real hermes home (the update-pull
# marker under the gitdir), which ``tests/home_io_guard.py`` rightly refuses.
# The instance carries exactly the state ``_handle_stream_error`` reads before
# reaching the log site; neighbours that own other decisions (channel switch,
# the non-streaming probe) are stubbed so the test pins THIS site's routing.

def _streaming_call():
    agent = SimpleNamespace(
        provider="custom", model="m", base_url="http://127.0.0.1:1/v1", api_mode="chat_completions",
        _is_provider_stream_parse_error=lambda e: False,
    )
    call = helpers._StreamingCall.__new__(helpers._StreamingCall)
    call.agent = agent
    call.result = {}
    call._request_cancelled = {"value": False}
    call.deltas_were_sent = {"yes": False}
    call._forget_invisible_deltas = lambda e: False
    return call


def test_handle_stream_error_routes_capacity_to_a_bare_warning(logged, monkeypatch):
    call = _streaming_call()
    monkeypatch.setattr(call, "_unmask_server_error_with_nonstreaming", lambda e: False)
    error = _capacity_error()
    assert call._handle_stream_error(error, attempt=0, max_retries=1) is False
    assert call.result["error"] is error, "propagation to the outer loop is unchanged"
    assert not [r for r in logged.records if r.levelno >= logging.ERROR], \
        "the retried capacity error must produce no ERROR record at the stream site"
    warnings = [r for r in logged.records
                if r.levelno == logging.WARNING and "Streaming failed before delivery" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].exc_info is None


def test_handle_stream_error_keeps_traceback_for_non_retryable(logged, monkeypatch):
    call = _streaming_call()
    monkeypatch.setattr(call, "_unmask_server_error_with_nonstreaming", lambda e: False)
    error = _auth_error()
    assert call._handle_stream_error(error, attempt=0, max_retries=1) is False
    assert call.result["error"] is error
    errors = [r for r in logged.records
              if r.levelno == logging.ERROR and "Streaming failed before delivery" in r.getMessage()]
    assert len(errors) == 1
    assert errors[0].exc_info is not None
