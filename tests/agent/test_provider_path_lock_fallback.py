"""A locked path walks the existing fallback chain, and when every rung is locked the chat says why.

The edge case the design is built around (Evan, 2026-11-20): a fallback model sitting on the SAME
locked path hits the same shared dial check, fails fast, and the existing loop simply moves to the
next rung. Nothing here adds a routing layer — the lock arrives as the provider-unavailability
error the fallback walk already handles, and the terminal message is the reason's chat copy.
"""

from unittest.mock import patch

import pytest

from agent.error_classifier import FailoverReason
from agent.provider_locks import (
    ProviderPathLocked, all_paths_locked_copy, create_lock, dial, list_locks,
)
from agent.turn_failure_copy import nonretryable_copy


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class _Route:
    """The facts the dial check reads off the agent: which path this route egresses on."""

    def __init__(self, provider):
        self.provider = provider


def _client():
    client = type("Client", (), {})()
    client.base_url = "https://example.test/v1"
    client.api_key = "test-key"
    client.chat = type("Chat", (), {})()
    client.chat.completions = type("Completions", (), {})()
    client.chat.completions.create = lambda *a, **kw: pytest.fail("a locked path must never dial upstream")
    return client


def _make_agent(fallback_model):
    from run_agent import AIAgent
    with (
        patch("model_tools.get_tool_definitions", return_value=[]),
        patch("model_tools.check_toolset_requirements", return_value={}),
        patch("agent.process_bootstrap.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key", provider="openrouter", base_url="https://openrouter.example.test/v1",
            model="test/test-model", quiet_mode=True, skip_context_files=True, skip_memory=True,
            fallback_model=fallback_model,
        )
    agent.client = None
    return agent


# ── the dial check itself ────────────────────────────────────────────────

class TestDialCheck:
    def test_a_healthy_path_dials(self, home):
        dial("openrouter")

    def test_a_locked_path_refuses_without_dialing(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="out of credits")
        with pytest.raises(ProviderPathLocked):
            dial("openrouter")

    def test_the_refusal_names_every_locked_path(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="org budget cap")
        create_lock(provider="claude-sub", lock_class="rate_limit", reason="reset hourly", ttl_seconds=900)
        with pytest.raises(ProviderPathLocked) as exc:
            dial("openrouter")
        assert "openrouter" in str(exc.value) and "claude-sub" in str(exc.value)


# ── fallback-on-lock routing: the real chain, rung by rung ───────────────

class TestFallbackOnLockRouting:
    def test_a_fallback_on_the_same_locked_path_advances_to_the_next_rung(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="org budget cap")
        create_lock(provider="anthropic", lock_class="billing", reason="no credits left")
        agent = _make_agent(fallback_model=[
            {"provider": "openrouter", "model": "test/same-path"},   # rung 0: the SAME locked path
            {"provider": "anthropic", "model": "test/other-path"},   # rung 1: also locked
        ])
        with patch("agent.auxiliary_client.resolve_provider_client", return_value=(_client(), "test/same-path")):
            refused = 0
            # Rung 0: still openrouter — its dial hits the same check and fails fast.
            assert agent._try_activate_fallback(reason=FailoverReason.provider_path_locked) is True
            assert agent.provider == "openrouter"
            with pytest.raises(ProviderPathLocked):
                dial(agent.provider)
            refused += 1
            # Rung 1: the loop moved on, and that path is locked too.
            assert agent._try_activate_fallback(reason=FailoverReason.provider_path_locked) is True
            assert agent.provider == "anthropic"
            with pytest.raises(ProviderPathLocked):
                dial(agent.provider)
            refused += 1
            assert refused == 2
            # Nothing left to hand the turn to: the loop's exhausted-chain path owns the message.
            assert agent._has_pending_fallback() is False

    def test_no_fallback_means_the_terminal_copy_is_what_the_user_sees(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="org budget cap")
        agent = _make_agent(fallback_model=[])
        assert agent._try_activate_fallback(reason=FailoverReason.provider_path_locked) is False


# ── the all-locked message ───────────────────────────────────────────────

class TestAllLockedCopy:
    def test_names_each_path_class_and_reason(self, home):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        copy = all_paths_locked_copy(list_locks())
        assert "claude-sub" in copy and "billing" in copy and "monthly spend limit" in copy

    def test_says_the_lock_is_deliberate_not_an_outage_to_ride_out(self, home):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        copy = all_paths_locked_copy(list_locks()).lower()
        assert "repeated failures" in copy and "retry storm" in copy
        assert "not an outage" in copy or "deliberately" in copy

    def test_names_the_exact_reopen_command_and_the_listing(self, home):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        copy = all_paths_locked_copy(list_locks())
        assert "hermes providers --reopen claude-sub" in copy
        assert "(list: hermes providers)" in copy

    def test_offers_a_command_per_locked_path(self, home):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        create_lock(provider="openrouter", lock_class="rate_limit", reason="reset hourly", ttl_seconds=900)
        copy = all_paths_locked_copy(list_locks())
        assert "hermes providers --reopen claude-sub" in copy
        assert "hermes providers --reopen openrouter" in copy
        assert copy.count("hermes providers --reopen") == 2

    def test_terminal_chat_copy_carries_it_without_a_provider_said_tail(self, home):
        from agent.error_classifier import ClassifiedError
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        classified = ClassifiedError(reason=FailoverReason.provider_path_locked, retryable=False,
                                     should_fallback=True, message="locked")
        copy = nonretryable_copy(classified, provider="openrouter", model="test/model",
                                summary="provider path locked until cleared: billing …")
        assert "hermes providers --reopen claude-sub" in copy
        # The lock is Hermes' own decision: appending the provider's words would blame the wrong party.
        assert "Provider said:" not in copy

    def test_no_locks_yields_no_lockout_message(self, home):
        assert all_paths_locked_copy([]) == ""
