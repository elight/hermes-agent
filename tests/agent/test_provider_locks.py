"""Per-path provider locks (agent/provider_locks.py) — the durable shared truth every dial site reads.

The 2026-11-20 storm: one Claude account's monthly spend limit was read as a transient 429, so
every session backed off and retried the same dead path. A lock says, in one file on disk, that a
provider path is closed until a human reopens it, and every process on the host obeys it.
"""

import json
import os
import time

import pytest

from agent.error_classifier import ClassifiedError, FailoverReason
from agent.provider_locks import (
    TRANSIENT_LOCK_MIN_SECONDS, ProviderPathLocked, clear_lock, create_lock, dial,
    list_locks, maybe_lock_from_error, path_id, read_lock, resolve_path,
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


def _classified(reason, *, message="", error_context=None):
    return ClassifiedError(reason=reason, message=message, error_context=dict(error_context or {}))


def _lock_dir(home):
    return home / "providers"


def _advance_clock(monkeypatch, seconds):
    """Move the clock forward for the module under test (the lambda must close over the real
    ``time.time``, or patching it makes the lambda call itself)."""
    real_time = time.time
    monkeypatch.setattr("time.time", lambda: real_time() + seconds)


# ── path identity ─────────────────────────────────────────────────────────

class TestPathId:
    def test_provider_alone_is_the_path(self):
        assert path_id("openrouter") == "openrouter"

    def test_auth_label_extends_the_path(self):
        # The same provider reached two ways bills two ways: they are two paths.
        assert path_id("anthropic", auth="oauth") == "anthropic-oauth"

    def test_slug_is_sanitized_not_injected(self):
        assert path_id("../../etc/passwd") == "etc_passwd"
        assert path_id("Claude Sub!") == "claude_sub"

    def test_empty_provider_has_no_path(self):
        assert path_id("") == ""


# ── lock file: existence, atomic create, contents ─────────────────────────

class TestCreateLock:
    def test_creates_the_named_file_with_display_fields(self, home):
        lock = create_lock(provider="claude-sub", lock_class="billing",
                           reason="monthly spend limit", source="HTTP 502: 429 monthly limit",
                           component="agent-loop")
        path = _lock_dir(home) / "claude-sub.lock"
        assert lock is not None and path.exists()
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["path"] == "claude-sub"
        assert raw["class"] == "billing"
        assert raw["reason"] == "monthly spend limit"
        assert raw["source"].startswith("HTTP 502")
        assert raw["component"] == "agent-loop"
        assert raw["locked_at"] == pytest.approx(time.time(), abs=5)
        assert raw["expires_at"] is None  # billing never expires on its own

    def test_no_temp_file_is_left_behind(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="out of credits")
        assert [p.name for p in _lock_dir(home).iterdir()] == ["openrouter.lock"]

    def test_first_writer_wins_so_the_cause_stays_readable(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="org budget cap")
        again = create_lock(provider="openrouter", lock_class="rate_limit", reason="later noise")
        assert again is None
        first = read_lock("openrouter")
        assert first is not None and first.reason == "org budget cap"

    def test_transient_lock_records_its_expiry(self, home):
        lock = create_lock(provider="openrouter", lock_class="rate_limit", reason="reset 1h",
                           ttl_seconds=900)
        assert lock is not None
        assert lock.expires_at == pytest.approx(time.time() + 900, abs=5)


# ── the dial-site question: is this path open? ────────────────────────────

class TestGuard:
    def test_locked_path_refuses_before_the_dial(self, home):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        with pytest.raises(ProviderPathLocked) as exc:
            dial("claude-sub")
        # Fail-fast message names the class, the reason and when, per the agreed contract.
        message = str(exc.value)
        assert message.startswith("provider path locked until cleared: billing monthly spend limit ")
        assert "20" in message  # a timestamp, not a bare sentence
        assert exc.value.lock.lock_class == "billing"

    def test_open_path_passes(self, home):
        dial("openrouter")

    def test_unknown_or_empty_provider_passes(self, home):
        dial("")
        dial("no-such-provider")

    def test_billing_lock_never_ages_out(self, home, monkeypatch):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        _advance_clock(monkeypatch, 60 * 60 * 24 * 365 * 10)
        with pytest.raises(ProviderPathLocked):
            dial("claude-sub")  # a billing lock waits for a human, never for a window

    def test_transient_lock_expires_and_the_file_goes_away(self, home, monkeypatch):
        create_lock(provider="openrouter", lock_class="rate_limit", reason="reset in 10m",
                    ttl_seconds=600)
        _advance_clock(monkeypatch, 601)
        dial("openrouter")  # window passed: the path is dialable again
        assert read_lock("openrouter") is None
        assert not (_lock_dir(home) / "openrouter.lock").exists()

    def test_a_corrupt_lock_never_strands_the_fleet(self, home):
        _lock_dir(home).mkdir(parents=True)
        (_lock_dir(home) / "openrouter.lock").write_text("{not json", encoding="utf-8")
        dial("openrouter")
        assert read_lock("openrouter") is None


# ── reopen (the operator's only way back) ─────────────────────────────────

class TestClearLock:
    def test_reopen_unlinks_and_the_next_dial_proceeds(self, home):
        create_lock(provider="openrouter", lock_class="billing", reason="out of credits")
        assert clear_lock("openrouter") is True
        dial("openrouter")

    def test_reopen_by_provider_name_when_only_one_path_carries_it(self, home):
        create_lock(provider="anthropic", auth="oauth", lock_class="billing", reason="no credits")
        assert resolve_path("anthropic") == "anthropic-oauth"
        assert clear_lock("anthropic") is True

    def test_reopen_is_ambiguous_rather_than_guessing(self, home):
        create_lock(provider="anthropic", auth="oauth", lock_class="billing", reason="no credits")
        create_lock(provider="anthropic", auth="api_key", lock_class="billing", reason="no credits")
        assert resolve_path("anthropic") is None
        with pytest.raises(ValueError):
            clear_lock("anthropic")

    def test_reopening_an_open_path_reports_false(self, home):
        assert clear_lock("openrouter") is False


# ── listing for `hermes providers` and the dashboard ─────────────────────

class TestListLocks:
    def test_lists_every_locked_path_stably_with_an_age(self, home, monkeypatch):
        create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit")
        _advance_clock(monkeypatch, 3700)
        create_lock(provider="openrouter", lock_class="rate_limit", reason="reset hourly",
                    ttl_seconds=900)
        locks = list_locks()
        assert [lock.path for lock in locks] == ["claude-sub", "openrouter"]
        assert locks[0].age_seconds == pytest.approx(3700, abs=5)
        assert locks[1].expires_at is not None

    def test_expired_locks_are_skipped_and_cleaned(self, home, monkeypatch):
        create_lock(provider="openrouter", lock_class="rate_limit", reason="reset in 10m",
                    ttl_seconds=600)
        _advance_clock(monkeypatch, 601)
        assert list_locks() == []


# ── which classified errors close a path ─────────────────────────────────

class TestTripFromClassifiedError:
    def test_billing_closes_the_path(self, home):
        lock = maybe_lock_from_error(
            _classified(FailoverReason.billing, message="insufficient credits (monthly spend limit)"),
            provider="claude-sub", component="agent-loop")
        assert lock is not None and lock.lock_class == "billing"
        assert lock.reason and lock.source
        with pytest.raises(ProviderPathLocked):
            dial("claude-sub")

    def test_ordinary_transient_429_does_not_close_the_path(self, home):
        # Per-request backoff is already the right tool; a lock here would punish healthy traffic.
        assert maybe_lock_from_error(
            _classified(FailoverReason.rate_limit, message="Rate limit exceeded"),
            provider="openrouter") is None
        assert list_locks() == []

    def test_short_named_reset_does_not_close_the_path(self, home):
        assert maybe_lock_from_error(
            _classified(FailoverReason.rate_limit, message="too many requests",
                        error_context={"reset_at": time.time() + 30}),
            provider="openrouter") is None

    def test_long_named_reset_closes_it_until_the_reset(self, home):
        reset_at = time.time() + TRANSIENT_LOCK_MIN_SECONDS + 600
        lock = maybe_lock_from_error(
            _classified(FailoverReason.rate_limit, message="quota exceeded, resets hourly",
                        error_context={"reset_at": reset_at}),
            provider="openrouter")
        assert lock is not None and lock.lock_class == "rate_limit"
        assert lock.expires_at == pytest.approx(reset_at, abs=5)

    @pytest.mark.parametrize("reason", [
        FailoverReason.upstream_rate_limit,  # the aggregator's key is healthy; our path is fine
        FailoverReason.server_error, FailoverReason.overloaded, FailoverReason.timeout,
        FailoverReason.auth, FailoverReason.model_not_found, FailoverReason.unknown,
    ])
    def test_transient_and_key_level_reasons_never_close_a_path(self, home, reason):
        assert maybe_lock_from_error(_classified(reason, message="nope"), provider="openrouter") is None

    def test_an_unverified_billing_reading_does_not_close_a_path(self, home):
        # #82154: Anthropic answers a content-filter rejection with the same "out of extra usage"
        # body. The pool coasts on that; closing a shared path on it would punish every session.
        assert maybe_lock_from_error(
            _classified(FailoverReason.billing, message="out of extra usage",
                        error_context={"billing_unverified": True}),
            provider="claude-sub") is None
        assert list_locks() == []

    def test_an_empty_provider_closes_nothing(self, home):
        assert maybe_lock_from_error(_classified(FailoverReason.billing), provider="") is None

    def test_a_write_failure_degrades_to_no_lock_instead_of_breaking_the_turn(self, home):
        # A read-only or full home must cost us the lock, never the reply.
        create_lock(provider="openrouter", lock_class="billing", reason="first cause")
        os.chmod(_lock_dir(home), 0o500)
        try:
            assert create_lock(provider="openrouter", lock_class="rate_limit", reason="later") is None
        finally:
            os.chmod(_lock_dir(home), 0o700)
