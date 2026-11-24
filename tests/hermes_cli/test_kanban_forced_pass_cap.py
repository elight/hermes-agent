"""A forced dispatch pass must mean what the gateway means by the same config key.

Four places start a dispatcher pass. The gateway tick and the CLI read
``kanban.max_in_progress_per_profile``; ``run_daemon`` and the dashboard's ``POST /dispatch``
passed no per-profile cap at all, so on a box where the key IS configured, those two could
start one profile's ready queue unchecked — an operator nudging the dispatcher by hand got a
different answer than the tick they were waiting for, and the fan-out each worker pays for is
a full model context.

The key's documented default stays *unset (unlimited)*
(``website/docs/user-guide/features/kanban.md``), so nothing here invents a cap the operator
did not configure; ``--max`` remains the way to bound one pass by hand. What did change: the
key is honoured on every path, a pass prints which cap it ran under, and a config that cannot
be READ stops the pass instead of unlocking it — because "could not read" is not the same
statement as "configured nothing".
"""
from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import threading

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME + kanban DB, and an ``alpha`` profile the spawner accepts."""
    test_home = tempfile.mkdtemp(prefix="kanban_forced_pass_cap_")
    for prof in ("alpha", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
        with open(os.path.join(test_home, "profiles", prof, "config.yaml"), "w") as fh:
            fh.write("{}\n")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if mod.startswith("hermes_cli") or mod.startswith("hermes_state") or mod == "hermes_constants":
            del sys.modules[mod]
    yield test_home


@pytest.fixture()
def capped_env(isolated_kanban_home, monkeypatch):
    """Real dispatcher, stubbed spawner, no host memory pressure capping the tick."""
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    spawned: list[str] = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(task.id)
        return 424000 + len(spawned)

    monkeypatch.setattr(kbd, "_default_spawn", fake_spawn)

    def make_ready(n, assignee="alpha"):
        with kbc.connect_closing() as conn:
            for i in range(n):
                kb.create_task(conn, title=f"ready {i}", assignee=assignee)
            # Pin the lane explicitly rather than trusting create_task's defaults: the
            # assertion is about how many workers ONE pass starts from a ready queue.
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status = 'ready'")
                conn.execute("UPDATE tasks SET consecutive_failures = 0")

    return make_ready, spawned


def _dispatch_cli(monkeypatch, args=None):
    """Run the real ``hermes kanban dispatch`` verb.

    Returns ``(exit_code, dispatch_once_kwargs, stdout + stderr)``. Both streams are captured
    here rather than via ``capsys`` because the verb prints its summary to stdout and its
    warnings/refusals to stderr; kwargs stay empty if the verb refused to dispatch at all,
    which is itself an assertion worth making.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    captured: dict = {}
    real = kbd.dispatch_once

    def spy(conn, **kwargs):
        captured.update(kwargs)
        return real(conn, **kwargs)

    monkeypatch.setattr(kbd, "dispatch_once", spy)
    from hermes_cli import kanban_ops

    ns = argparse.Namespace(dry_run=False, max=None, failure_limit=2, json=False)
    if args:
        vars(ns).update(args)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = kanban_ops._cmd_dispatch(ns)
    return rc, captured, out.getvalue() + err.getvalue()


BROKEN_YAML = "kanban:\n  max_in_progress_per_profile: 1\n  bad: [unclosed\n"


def _write_raw_config(home, text):
    """Write config.yaml the way an operator's editor would: raw bytes, no schema in between."""
    from hermes_cli.config import get_config_path
    path = get_config_path() if home is None else os.path.join(home, "config.yaml")
    with open(path, "w") as fh:
        fh.write(text)
    return path


# --------------------------------------------------------------------------- #
# The cap means the same thing on every path                                  #
# --------------------------------------------------------------------------- #

def test_configured_cap_holds_on_a_forced_pass(isolated_kanban_home, capped_env, monkeypatch):
    """Five ready cards, one profile, one real pass, cap configured ⇒ one worker.

    Driven through the real ``dispatch_once`` with only the spawner stubbed, so the assertion
    is the number of workers started, not a kwarg someone remembered to forward.
    """
    make_ready, spawned = capped_env
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress_per_profile": 2}})
    make_ready(5)
    _dispatch_cli(monkeypatch)
    assert len(spawned) == 2, (
        f"a configured per-profile cap of 2 started {len(spawned)} workers in one pass"
    )


def test_daemon_and_dashboard_honour_the_configured_cap(isolated_kanban_home, monkeypatch):
    """The two paths that passed no per-profile cap at all now forward the configured one.

    ``run_daemon``'s own docstring: "the standalone daemon must not be the one uncapped entry
    point." Before this it was, and so was the dashboard nudge (default ``max=8``).
    """
    from hermes_cli import kanban_db
    from hermes_cli import kanban_db_dispatch as kbd

    cfg = lambda: {"kanban": {"max_in_progress_per_profile": 3}}
    monkeypatch.setattr("hermes_cli.config.load_config", cfg)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", cfg)

    daemon_kwargs: dict = {}
    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kw: (daemon_kwargs.update(kw), kanban_db.DispatchResult())[1],
    )
    stop = threading.Event()
    kbd.run_daemon(interval=0.05, stop_event=stop, on_tick=lambda _res: stop.set())
    assert daemon_kwargs.get("max_in_progress_per_profile") == 3, (
        "the daemon tick ignored kanban.max_in_progress_per_profile; "
        f"got {daemon_kwargs.get('max_in_progress_per_profile')!r}"
    )

    pytest.importorskip("fastapi", reason="dashboard route needs fastapi")
    from plugins.kanban.dashboard import plugin_api as api

    nudge_kwargs: dict = {}
    monkeypatch.setattr(
        api.kbd, "dispatch_once",
        lambda conn, **kw: (nudge_kwargs.update(kw), kanban_db.DispatchResult())[1],
    )

    @contextlib.contextmanager
    def fake_board_conn(_board):
        yield "default", None

    monkeypatch.setattr(api, "_board_conn", fake_board_conn)
    api.dispatch(dry_run=True, max_n=8, board=None)
    assert nudge_kwargs.get("max_in_progress_per_profile") == 3, (
        "the dashboard nudge ignored the per-profile cap; "
        f"got {nudge_kwargs.get('max_in_progress_per_profile')!r}"
    )


def test_unset_key_stays_unlimited_on_every_path(isolated_kanban_home, monkeypatch):
    """No cap configured ⇒ unlimited, on the CLI too. That is the documented default.

    Guard against the tempting wrong fix: defaulting the forced path alone to something
    conservative would make the CLI and the gateway disagree about one key, which is the
    very defect being fixed here.
    """
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    _rc, captured, out = _dispatch_cli(monkeypatch)
    assert captured.get("max_in_progress_per_profile") is None
    assert "unlimited" in out.lower(), (
        "an unlimited pass should say so, so the operator can tell 'no cap configured' "
        "from 'the cap said stop'"
    )


# --------------------------------------------------------------------------- #
# A config that cannot be read stops the pass                                 #
# --------------------------------------------------------------------------- #

def test_a_real_unparseable_config_stops_the_pass(isolated_kanban_home, capped_env, monkeypatch):
    """A broken config.yaml on disk, no last-known-good copy, nothing mocked about config.

    The whole point of this case: ``load_config()`` does NOT raise on bad YAML — it serves the
    documented fail-open fallback (``FailedConfigRead``, defaults standing in), so a guard written
    against an exception guards nothing. The operator here wrote
    ``max_in_progress_per_profile: 1``; a pass that reads that file as "unset" and dispatches is
    the fail-open this PR is meant to close.
    """
    make_ready, spawned = capped_env
    _write_raw_config(isolated_kanban_home, BROKEN_YAML)
    make_ready(5)
    rc, captured, out = _dispatch_cli(monkeypatch)
    assert captured == {}, (
        "dispatch_once ran against an unparseable config.yaml: "
        f"{captured.get('max_in_progress_per_profile')!r} — the loader serves a fail-open "
        "fallback rather than raising, so an `except Exception` never sees this"
    )
    assert spawned == [], f"a pass over an unreadable config started {len(spawned)} workers"
    assert rc != 0, "refusing to dispatch must be visible in the exit code"
    assert "cannot read config" in out.lower(), out


def test_a_recovered_copy_still_stops_the_pass(isolated_kanban_home, capped_env, monkeypatch):
    """Even a recoverable read refuses, because the caller cannot tell recovery from defaults.

    Measured on this repo's loader: break the YAML after a good load and ``load_config()`` still
    returns ``FailedConfigRead`` — with the operator's last good ``cap: 2`` sitting *inside* it.
    Break it with no copy available and it returns ``FailedConfigRead`` with defaults. The two
    arrive as the same type from outside, so "honour the recovered values but warn" cannot be
    implemented without new plumbing in the loader, and dispatching on the defaults half of that
    ambiguity is the fail-open. The refusal message names the recovery so the operator knows
    their settings are still on disk.
    """
    from hermes_cli.config import load_config

    make_ready, spawned = capped_env
    _write_raw_config(isolated_kanban_home,
                      "kanban:\n  max_in_progress_per_profile: 2\n")
    load_config()  # a successful parse is what records the last-known-good copy
    _write_raw_config(isolated_kanban_home, BROKEN_YAML)
    make_ready(5)
    rc, captured, out = _dispatch_cli(monkeypatch)
    assert rc != 0, f"a read that failed must refuse the pass even when recoverable; rc={rc}"
    assert captured == {}, "dispatch_once ran on a FailedConfigRead that carried recovered values"
    assert spawned == [], f"started {len(spawned)} workers on a config that failed to read"
    assert "last-known-good" in out.lower(), (
        "the refusal should say the operator's settings are still recoverable, not just "
        "that they are broken: " + out
    )


def test_the_cap_helper_says_unreadable_rather_than_unset(isolated_kanban_home, caplog):
    """``run_daemon`` and the dashboard must not log 'unset' for a file that failed to read.

    They are allowed to keep dispatching on a broken config (the daemon tick should not die
    because config.yaml is unhappy), but 'nothing configured' and 'I could not read it' cannot
    be the same line in the log, or the next incident is un-debuggable.
    """
    import logging

    from hermes_cli import kanban_dispatch_cap

    _write_raw_config(isolated_kanban_home, BROKEN_YAML)
    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_dispatch_cap"):
        assert kanban_dispatch_cap.configured_profile_cap() is None
    assert any("could not read" in r.message.lower() or "unreadable" in r.message.lower()
               for r in caplog.records), (
        "a FailedConfigRead was reported as merely unset; the two must be distinguishable"
    )


# --------------------------------------------------------------------------- #
# What the operator sees                                                      #
# --------------------------------------------------------------------------- #

def test_zero_stays_an_explicit_optout(isolated_kanban_home, monkeypatch):
    """``max_in_progress_per_profile: 0`` means "no cap" and must keep meaning that."""
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress_per_profile": 0}})
    _rc, captured, out = _dispatch_cli(monkeypatch)
    assert captured.get("max_in_progress_per_profile") is None
    assert "unlimited" in out.lower()


def test_the_pass_names_the_cap_it_ran_under(isolated_kanban_home, monkeypatch):
    """A configured cap is reported as a number and attributed to its key, in text and JSON."""
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress_per_profile": 3}})
    _rc, _captured, out = _dispatch_cli(monkeypatch)
    assert "per-profile cap: 3" in out.lower(), out

    import json

    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    _rc, _c, out_json = _dispatch_cli(monkeypatch, args={"json": True})
    payload = json.loads(out_json)
    assert payload["per_profile_cap"] is None
    assert payload["per_profile_cap_source"] == "unset", (
        "--json should distinguish 'unset' from 'opted-out' from a configured number"
    )
