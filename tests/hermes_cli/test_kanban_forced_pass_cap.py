"""A forced dispatch pass must never fan out unbounded for one profile.

``hermes kanban dispatch``, the deprecated ``kanban daemon`` loop and the dashboard's
``POST /dispatch`` nudge all build a ``dispatch_once`` call by hand. Only the CLI reads
``kanban.max_in_progress_per_profile``, and when that key does not resolve its fallback is
*no cap at all*: five ready cards for one profile became five workers in one pass. The
gateway tick is the only path that resolves the cap, and even it depends on the config
being readable.

Core already refuses the analogous hazard for the *global* cap — ``resolve_max_in_progress``
applies a memory-derived default because "unbounded fan-out swap-thrashes small hosts"
(``gateway/kanban_watchers_dispatcher.py``). The per-profile cap had no such default. These
tests pin the fail-closed default, the two ways an operator deliberately opts out (``0`` in
config, ``--ignore-cap`` on the command line), and the sibling entry points.
"""
from __future__ import annotations

import argparse
import contextlib
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
        return spawned

    return make_ready, spawned


def _dispatch_cli(monkeypatch, args=None):
    """Run the real ``hermes kanban dispatch`` verb.

    Returns ``(dispatch_once_kwargs, text_printed_to_stdout)`` — stdout is captured here
    rather than via ``capsys`` because the verb prints its own summary.
    """
    import io

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
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kanban_ops._cmd_dispatch(ns)
    return captured, buf.getvalue()


# --------------------------------------------------------------------------- #
# D1: no cap resolving must fail closed, not open                            #
# --------------------------------------------------------------------------- #

def test_forced_pass_defaults_the_per_profile_cap_when_the_key_is_unset(isolated_kanban_home,
                                                                       monkeypatch):
    """kanban.max_in_progress_per_profile absent ⇒ a forced pass still caps per profile.

    Absent is not "unlimited" for a one-shot pass an operator runs by hand: the whole point
    of the pass is to unstick a board, and the accident it causes is five workers at once.
    """
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress": 4}})
    captured, _out = _dispatch_cli(monkeypatch)
    assert captured.get("max_in_progress_per_profile") == 1, (
        "a forced pass with no per-profile cap configured must fall back to a conservative "
        f"default, not uncapped; got {captured.get('max_in_progress_per_profile')!r}"
    )


def test_forced_pass_does_not_fan_out_five_cards_for_one_profile(isolated_kanban_home,
                                                                capped_env,
                                                                monkeypatch):
    """The card's headline: 5 ready cards, one profile, ONE pass, cap unset ⇒ one worker.

    End-to-end through the real ``dispatch_once`` with a stubbed spawner — the number of
    workers actually started, not a kwarg someone remembered to forward.
    """
    make_ready, spawned = capped_env
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress": 4}})
    make_ready(5)
    _dispatch_cli(monkeypatch)
    assert len(spawned) == 1, (
        f"one forced pass started {len(spawned)} workers for a single profile; the cap "
        "must hold whether or not the operator remembered to configure it"
    )


def test_unreadable_config_fails_closed_not_uncapped(isolated_kanban_home, monkeypatch):
    """A config that raises must not silently disable the per-profile cap.

    ``_cmd_dispatch`` wraps its whole config read in ``except Exception`` and nulls every
    cap on the way out; a broken config then reads as "spawn everything".
    """
    def boom():
        raise RuntimeError("config.yaml is unparseable")

    monkeypatch.setattr("hermes_cli.config.load_config", boom)
    captured, _out = _dispatch_cli(monkeypatch)
    assert captured.get("max_in_progress_per_profile") == 1, (
        "an unreadable config left the per-profile cap "
        f"{captured.get('max_in_progress_per_profile')!r}; fail closed"
    )


# --------------------------------------------------------------------------- #
# The operator's two deliberate escapes stay deliberate                       #
# --------------------------------------------------------------------------- #

def test_configured_cap_still_wins(isolated_kanban_home, monkeypatch):
    """A configured cap is never overridden by the new default."""
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress_per_profile": 3}})
    captured, _out = _dispatch_cli(monkeypatch)
    assert captured.get("max_in_progress_per_profile") == 3


def test_zero_stays_an_explicit_optout(isolated_kanban_home, monkeypatch):
    """``max_in_progress_per_profile: 0`` means "no cap" and must keep meaning that."""
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress_per_profile": 0}})
    captured, _out = _dispatch_cli(monkeypatch)
    assert captured.get("max_in_progress_per_profile") is None


def test_ignore_cap_bypasses_and_says_so(isolated_kanban_home, capped_env, monkeypatch):
    """``--ignore-cap`` is the only way to start past the cap, and it announces itself."""
    make_ready, spawned = capped_env
    # No global cap in this config: the per-profile cap is then the ONLY thing that can
    # stop these five, which is exactly what --ignore-cap gives up.
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"kanban": {}})
    make_ready(5)
    captured, out = _dispatch_cli(monkeypatch, args={"ignore_cap": True})
    assert captured.get("max_in_progress_per_profile") is None, (
        "--ignore-cap must actually lift the cap, or the flag is a lie"
    )
    assert len(spawned) == 5, f"--ignore-cap should start all five; started {len(spawned)}"
    assert "ignore-cap" in out.lower(), (
        "a deliberate bypass must be visible on stdout; the operator who wants one should "
        "see what they gave up"
    )


def test_defaulted_cap_is_announced(isolated_kanban_home, monkeypatch):
    """A defaulted cap is reported, so 'why did only one start' is answerable from the run."""
    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress": 4}})
    _captured, out = _dispatch_cli(monkeypatch)
    assert "per-profile" in out.lower(), (
        "the pass should state the per-profile cap it applied and that it was defaulted"
    )


# --------------------------------------------------------------------------- #
# The sibling entry points (same class, same fix)                             #
# --------------------------------------------------------------------------- #

def test_standalone_daemon_tick_applies_the_per_profile_cap(isolated_kanban_home, monkeypatch):
    """``run_daemon``'s own docstring: "the standalone daemon must not be the one uncapped
    entry point." It currently passes no per-profile cap at all."""
    from hermes_cli import kanban_db_dispatch as kbd

    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress": 4}})
    # The daemon reads the cap through the read-only loader (as the gateway does); patch
    # that one too, or the test would only be proving the fallback of an empty home.
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"kanban": {"max_in_progress": 4}})
    captured: dict = {}
    monkeypatch.setattr(
        kbd, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), _empty_result(kbd))[1],
    )
    stop = threading.Event()

    def on_tick(_res):
        stop.set()

    kbd.run_daemon(interval=0.05, stop_event=stop, on_tick=on_tick)
    assert captured.get("max_in_progress_per_profile") == 1, (
        "the daemon tick resolved no per-profile cap; it is the uncapped entry point its "
        "docstring forbids"
    )


def _empty_result(kbd):
    from hermes_cli import kanban_db
    return kanban_db.DispatchResult()


def test_dashboard_dispatch_nudge_applies_the_per_profile_cap(isolated_kanban_home, monkeypatch):
    """``POST /dispatch`` is the same gesture as the CLI pass, one click away, default
    ``max=8`` — and it passed no per-profile cap either."""
    pytest.importorskip("fastapi", reason="dashboard route needs fastapi")
    from hermes_cli import kanban_db
    from plugins.kanban.dashboard import plugin_api as api

    monkeypatch.setattr("hermes_cli.config.load_config",
                        lambda: {"kanban": {"max_in_progress": 4}})
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda: {"kanban": {"max_in_progress": 4}})
    captured: dict = {}
    monkeypatch.setattr(
        api.kbd, "dispatch_once",
        lambda conn, **kw: (captured.update(kw), kanban_db.DispatchResult())[1],
    )

    @contextlib.contextmanager
    def fake_board_conn(_board):
        yield "default", None

    monkeypatch.setattr(api, "_board_conn", fake_board_conn)
    api.dispatch(dry_run=True, max_n=8, board=None)
    assert captured.get("max_in_progress_per_profile") == 1, (
        "the dashboard nudge must respect the same per-profile cap as every other path; "
        f"got {captured.get('max_in_progress_per_profile')!r}"
    )
