"""The updater's interactive prompts must not outlive their deadline (2026-11-12 fleet fence).

While ``hermes update`` holds its marker + checkout flock the fence is fleet-wide: for every profile
in the install ``gateway_runtime._update_fenced()`` is True and ``_fenced_verdict()`` answers
``draining/update_paused``, so every cron run and every kanban worker admission is refused. A
non-gateway run also ignores SIGHUP (``main_dashboard._install_hangup_protection``), so abandoning the
terminal cannot end that fence.

Measured: on 2026-11-12 one *no-op* update (``pre_update.sha == post_update.sha``) held the lock for
12 h 04 m — 11 h 59 m 58 s of it parked in a bare ``input()`` at ``Restore local changes now? [Y/n]``
(``update_cmd_stash._confirm_restore``, which had no bound; its ``--gateway`` sibling
``update_cmd._gateway_prompt`` is bounded at 300 s). 82 cron executions were declined in that window.

``hermes_cli.update_cmd_wait`` bounds the RUN's stdin instead of one call site per prompt, so these
tests drive the prompts themselves — unpatched, through the real ``input()`` — with a stubbed clock, a
stubbed reader and no sleeps.
"""

from __future__ import annotations

import queue
import sys

import pytest

from hermes_cli import main_dashboard, update_cmd_config, update_cmd_stash


class _WouldBlock(Exception):
    """A read that would park on a stdin nobody answers — the pre-fix shape of the defect."""


class _StubClock:
    """This run's clock: one second of wall clock per read, so the bound is real time and no test
    sleeps. Elapsed "seconds" therefore count reads — including the read that starts a wait."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class _Terminal:
    """The run's stream. A test never lets a read reach it (the reader is injected); at the pre-fix
    revision a prompt reads it directly and fails here instead of parking the suite."""

    def isatty(self) -> bool:
        return True

    def readline(self, *_args) -> str:
        raise _WouldBlock("the prompt read stdin with no deadline")


def _bounded_run(monkeypatch, *, timeout=900.0, answer=None):
    """Point this run's stdin at a stub clock and a stub terminal, the way the update installs it.

    ``answer=None`` models a terminal nobody ever answers; a string models what the terminal's next
    ``readline()`` returns (``"\\n"`` = a bare Enter, ``""`` = the stream is closed). Returns
    ``(module, clock, reader)``, ``clock.now`` being the run's elapsed stub time. The reader is
    injected, so no thread starts and ``reader["box"]`` is the queue the run reads from — which lets
    a test type a line after the deadline has already passed. At the PRE-FIX revision there is no
    module to install, so the terminal above stays in place and every test fails behaviourally
    instead of erroring at collection: the red-on-base evidence.
    """
    terminal = _Terminal()
    monkeypatch.setattr(sys, "stdin", terminal)
    try:
        from hermes_cli import update_cmd_wait
    except ImportError:  # pre-fix revision: no bound, and no stubbed clock to measure it with
        return None, _StubClock(), {"box": None}

    clock = _StubClock()
    reader = {"box": None}

    def _start_reader(_source, box):
        """Stand in for the reader thread: what the terminal will have typed, and when."""
        reader["box"] = box
        if answer is not None:
            box.put(answer)

    monkeypatch.setattr(update_cmd_wait, "_POLL_SLICE_SECONDS", 0)  # a poll, without waiting
    monkeypatch.setattr(update_cmd_wait, "_start_reader", _start_reader)
    monkeypatch.setattr(sys, "stdin", update_cmd_wait.bounded_stdin(
        terminal, timeout=timeout, clock=clock))
    return update_cmd_wait, clock, reader


def _as_tty(monkeypatch):
    """The house isatty stub (``tests/hermes_cli/test_cli_light_mode.py``), for the prompt gates."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)


def test_an_unanswered_restore_prompt_gives_up_instead_of_blocking(monkeypatch, capsys):
    """The 2026-11-12 shape: a stashed run whose restore question is never answered."""
    _wait, clock, _reader = _bounded_run(monkeypatch)

    started = clock.now
    assert update_cmd_stash._confirm_restore("stash@{0}", None) is False

    assert 900.0 <= clock.now - started <= 901.0  # one deadline, + the read that starts it
    assert "Skipped restoring local changes." in capsys.readouterr().out


def test_the_wait_is_announced_while_it_happens(monkeypatch, capsys):
    """A run that is waiting must say so, from the outside: the deadline, each minute, and the end."""
    update_cmd_wait, _clock, _reader = _bounded_run(monkeypatch)

    update_cmd_stash._confirm_restore("stash@{0}", None)
    out = capsys.readouterr().out

    assert "(Waiting up to 15m for an answer" in out
    waiting = [line for line in out.splitlines() if "still waiting on stdin" in line]
    assert len(waiting) == 14, out  # every 60 s of a 900 s wait, the last a minute before the end
    assert waiting[0] == "  ⏳ still waiting on stdin (60s of 900s) — no answer yet"
    assert waiting[-1] == "  ⏳ still waiting on stdin (840s of 900s) — no answer yet"
    assert "  ⏱ No answer after 900s — continuing with this prompt's default" in out
    assert "Restore local changes now? [Y/n]" in out  # the human's question is unchanged
    assert update_cmd_wait.PROMPT_TIMEOUT_SECONDS == 900.0
    assert update_cmd_wait.NOTICE_INTERVAL_SECONDS == 60.0


def test_the_wait_lands_in_update_log_while_the_run_still_holds_the_fence(monkeypatch, tmp_path):
    """Observable from outside the run: the notice goes to stdout, which the update mirrors into the
    root home's ``logs/update.log`` — read here while the wait is still in progress."""
    import hermes_constants

    from hermes_cli import update_cmd_wait

    _module, clock, _reader = _bounded_run(monkeypatch)
    monkeypatch.setattr(update_cmd_wait, "bounded_stdin", lambda source=None: update_cmd_wait.BoundedStdin(
        _Terminal(), timeout=900.0, clock=clock))
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)

    state = main_dashboard._install_hangup_protection()
    try:
        assert sys.stdin.readline() == ""  # gives up, says so, and hands the prompt an EOF
        logged = (tmp_path / "logs" / "update.log").read_text(encoding="utf-8")
    finally:
        main_dashboard._finalize_update_output(state)

    assert "⏳ still waiting on stdin (60s of 900s) — no answer yet" in logged, logged
    assert "⏱ No answer after 900s" in logged, logged


def test_a_typed_answer_still_restores_or_skips_as_today(monkeypatch, capsys):
    """A human at a keyboard is not timed out: yes restores, no skips, and bare Enter restores."""
    for answer, expected in (("y", True), ("n", False), ("\n", True)):
        _bounded_run(monkeypatch, answer=answer)
        assert update_cmd_stash._confirm_restore("stash@{0}", None) is expected, answer
        assert "still waiting on stdin" not in capsys.readouterr().out


def test_a_closed_stdin_is_not_a_timeout(monkeypatch, capsys):
    """EOF is its own case: a closed stdin resolves at once, as the prompt's ``except EOFError`` did."""
    _wait, clock, _reader = _bounded_run(monkeypatch, answer="")

    started = clock.now
    assert update_cmd_stash._confirm_restore("stash@{0}", None) is False
    assert clock.now - started <= 2.0  # no deadline was waited for
    assert "still waiting on stdin" not in capsys.readouterr().out


def test_a_second_prompt_in_the_run_does_not_wait_again(monkeypatch, capsys):
    """One deadline per run: after a prompt gives up, later prompts answer EOF at once — and do not
    steal the line a reader is still holding."""
    _wait, clock, reader = _bounded_run(monkeypatch)

    assert update_cmd_stash._confirm_restore("stash@{0}", None) is False
    waited = clock.now

    reader["box"].put("y\n")  # the answer arrives after the deadline: it is not waited for
    assert update_cmd_stash._confirm_restore("stash@{0}", None) is False
    assert clock.now == waited  # the second prompt ran out no clock at all
    assert reader["box"].qsize() == 1
    assert "this run will not wait on stdin again" in capsys.readouterr().out


def test_any_read_of_the_runs_stdin_is_bounded(monkeypatch):
    """The fence is held by the run, not by the updater's own prompts: a prompt from any module —
    the interactive config migration, or one a later revision adds — waits on the same deadline."""
    _wait, clock, _reader = _bounded_run(monkeypatch)

    started = clock.now
    with pytest.raises(EOFError):
        input("A prompt outside hermes_cli.update_cmd*: ")
    assert clock.now - started <= 901.0

    assert sys.stdin.read() == ""       # the other two ways text leaves this stream
    assert list(sys.stdin) == []


def test_the_deadline_bounds_the_config_prompt(monkeypatch, capsys):
    """Skipping is the safe default: "y" opens an interactive migration this run cannot serve."""
    _bounded_run(monkeypatch)
    _as_tty(monkeypatch)

    assert update_cmd_config._ask_configure_new_options(assume_yes=False, gateway_mode=False) == "n"
    assert "No answer after 900s" in capsys.readouterr().out


def test_the_deadline_bounds_the_upstream_prompt(monkeypatch, tmp_path, capsys):
    """The upstream-remote question sits on the same path: unanswered means no remote, no block."""
    from hermes_cli import update_cmd_git

    _bounded_run(monkeypatch)
    _as_tty(monkeypatch)

    assert update_cmd_git._offer_upstream_remote(["git"], tmp_path, assume_yes=False,
                                                input_fn=None) is False
    out = capsys.readouterr().out
    assert "No answer after 900s" in out
    assert "Adding upstream remote" not in out


def test_the_run_installs_and_restores_the_bounded_stdin(monkeypatch, tmp_path):
    """One install point, restored by ``_finalize_update_output`` — no run keeps a bound stdin."""
    import hermes_constants

    from hermes_cli import update_cmd_wait

    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda: tmp_path)
    real_stdin = sys.stdin

    state = main_dashboard._install_hangup_protection()
    try:
        assert isinstance(sys.stdin, update_cmd_wait.BoundedStdin)
        assert state["prev_stdin"] is real_stdin
    finally:
        main_dashboard._finalize_update_output(state)

    assert sys.stdin is real_stdin
