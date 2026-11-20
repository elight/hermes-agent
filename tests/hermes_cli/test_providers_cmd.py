"""``hermes providers`` — the operator's view of locked provider paths and the reopen path."""

import json
import types

import pytest

from agent.provider_locks import create_lock, list_locks
from hermes_cli.providers_cmd import cmd_providers


@pytest.fixture
def home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    # A test run has no terminal: the picker must not open, and must not crash.
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    return hermes_home


def _args(**overrides):
    base = {"reopen": None, "json": False, "no_select": False}
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _run(capsys, **overrides):
    rc = cmd_providers(_args(**overrides))
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def test_an_open_fleet_says_so(home, capsys):
    rc, out, _ = _run(capsys)
    assert rc == 0 and "No provider paths are locked" in out


def test_a_locked_path_is_listed_with_reason_age_and_its_command(home, capsys):
    create_lock(provider="claude-sub", lock_class="billing", reason="monthly spend limit",
                component="agent-loop")
    rc, out, _ = _run(capsys)
    assert rc == 0
    assert "claude-sub" in out
    assert "monthly spend limit" in out and "billing" in out
    assert "locked " in out and "ago" in out
    assert "hermes providers --reopen claude-sub" in out
    # The message must not read as an outage to ride out.
    assert "repeated failures" in out and "healthy" in out


def test_json_lists_the_same_facts_for_tooling(home, capsys):
    create_lock(provider="openrouter", lock_class="rate_limit", reason="rate limited until reset",
                ttl_seconds=900)
    rc, out, _ = _run(capsys, json=True)
    payload = json.loads(out)
    assert rc == 0 and [p["path"] for p in payload["locked_paths"]] == ["openrouter"]
    assert payload["locked_paths"][0]["expires_at"]


def test_reopen_clears_the_lock(home, capsys):
    create_lock(provider="openrouter", lock_class="billing", reason="out of credits")
    rc, out, _ = _run(capsys, reopen="openrouter")
    assert rc == 0 and "Reopened openrouter" in out
    assert list_locks() == []


def test_reopening_a_path_that_is_not_locked_reports_it(home, capsys):
    rc, _, err = _run(capsys, reopen="openrouter")
    assert rc == 1 and "no provider path is locked" in err


def test_an_ambiguous_name_is_returned_to_the_operator_rather_than_guessed(home, capsys):
    create_lock(provider="anthropic", auth="oauth", lock_class="billing", reason="no credits")
    create_lock(provider="anthropic", auth="api_key", lock_class="billing", reason="no credits")
    rc, _, err = _run(capsys, reopen="anthropic")
    assert rc == 2 and "anthropic-oauth" in err and "anthropic-api_key" in err
    assert len(list_locks()) == 2  # neither was guessed at
