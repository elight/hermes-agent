"""Bounded stdin for ``hermes update`` — a run holding the fleet fence never parks on a prompt.

While ``hermes update`` holds its marker plus the checkout flock the fence is FLEET-WIDE:
``gateway_runtime._update_fenced()`` is True, ``_fenced_verdict()`` answers
``GatewayDiscovery("draining", reason_code="update_paused")``, and every cron run and every kanban
worker admission in the install is refused. Measured 2026-11-12: a *no-op* update held that fence for
12 h 04 m — 11 h 59 m 58 s of it parked in a bare ``input()`` at ``Restore local changes now? [Y/n]``
— and 82 scheduled executions were declined in the window. A non-gateway run also ignores SIGHUP
(``main_dashboard._install_hangup_protection``), so abandoning the terminal could not end the wait,
and nothing logged it. ``--gateway`` bounds its relayed prompts at 300 s
(``update_cmd._gateway_prompt``); the local prompts had no bound at all.

The fence is held by the RUN, so the bound belongs to the run: :func:`bounded_stdin` wraps this run's
stdin, beside the stdout/stderr mirror in ``_install_hangup_protection`` and restored by
``_finalize_update_output``, and EVERY read through it is bounded — ``input()`` at any prompt, from
any module. Bounding the updater's own prompts one call site at a time is not the same fix: the
interactive config migration the update can open (``update_cmd_config`` →
``migrate_config(interactive=True)`` → ``hermes_cli/config.py``'s prompt reads) asks its questions from
inside the same fence, and a later revision can add a prompt neither of us edited.

The deadline is per read, and the run retires stdin after the first one, so a run extends the fence by
at most ``PROMPT_TIMEOUT_SECONDS`` in total: an overdue read answers ``""`` (EOF) — what each prompt's
existing ``except EOFError`` already treats as its non-destructive default (keep the stashed changes,
skip the restore, decline the wizard) — and every later read in that run answers EOF at once instead
of waiting again. What the user sees while it waits (the deadline on the prompt, a ``still waiting on
stdin (…s)`` line every minute, the ``⏱`` line when it gives up) goes to stdout, which the update
mirrors into ``<root>/logs/update.log``, so the wait is observable from outside the run.
"""

from __future__ import annotations

import queue
import sys
import time

# A quarter-hour is 3x the ``--gateway`` relay bound: a human reading a stashed diff at a keyboard is
# not a messenger round trip, and 15 minutes stays under the ops threshold for a suspect fence.
PROMPT_TIMEOUT_SECONDS = 900.0
NOTICE_INTERVAL_SECONDS = 60.0
_POLL_SLICE_SECONDS = 0.5

_monotonic = time.monotonic


def _start_reader(source, box: queue.Queue) -> None:
    """Feed *box* from *source* on a daemon thread: one line per read, then the terminal exception.

    ``input()`` can only block the thread that calls it, so the wait has to happen off the prompt's
    thread for a deadline to exist at all. EOF is terminal — ``readline()`` returns ``""`` forever, so
    the loop stops there rather than spinning on it.
    """
    from agent.memory_provider import spawn_context_thread

    def _read() -> None:
        while True:
            try:
                line = source.readline()
            except Exception as exc:  # health: allow BLE001 -- the stream's own failure must reach the prompt's thread, which re-raises it exactly as a bare read would
                box.put(exc)
                return
            box.put(line)
            if not line:
                return

    spawn_context_thread(_read, name="hermes-update-stdin").start()


class BoundedStdin:
    """``sys.stdin`` for one update run: every read is bounded, announced, and EOF at the deadline.

    Drop-in for the wrapped stream — ``isatty``/``fileno``/everything unrecognised is delegated, so
    the prompts' own ``sys.stdin.isatty()`` gates and any other stream use behave as before.
    """

    def __init__(self, source, *, timeout: float = PROMPT_TIMEOUT_SECONDS,
                 notice_every: float = NOTICE_INTERVAL_SECONDS, clock=_monotonic):
        self._source = source
        self._timeout = timeout
        self._notice_every = notice_every
        self._clock = clock
        self._box: queue.Queue | None = None
        self._retired = False

    # -- the ways text leaves a stream, each bounded -------------------------------------------
    def readline(self, size: int = -1) -> str:  # a prompt passes no size hint
        return self._bounded_line()

    def read(self, size: int = -1) -> str:  # as readline: this run reads lines, never a block
        return self._bounded_line()

    def __iter__(self) -> BoundedStdin:
        return self

    def __next__(self) -> str:
        line = self._bounded_line()
        if not line:
            raise StopIteration
        return line

    # -- the bound ------------------------------------------------------------------------------
    def _bounded_line(self) -> str:
        if self._retired:
            return ""
        started = self._clock()
        deadline = started + self._timeout
        notice_at = started + self._notice_every
        self._say(f"  (Waiting up to {_duration(self._timeout)} for an answer — this update holds the "
                  f"install's update lock until the run ends.)")
        while True:
            now = self._clock()
            if now >= deadline:
                self._say(f"  ⏱ No answer after {int(self._timeout)}s — continuing with this prompt's "
                          f"default, and this run will not wait on stdin again.")
                self._retired = True  # one deadline per run, however many prompts follow it
                return ""
            if now >= notice_at:
                self._say(f"  ⏳ still waiting on stdin ({int(now - started)}s of "
                          f"{int(self._timeout)}s) — no answer yet")
                notice_at += self._notice_every
            line = self._next_line()
            if line is not None:
                return line

    def _next_line(self) -> str | None:
        """The next typed line, or ``None`` when none arrived within one poll slice.

        An empty line is a real answer (bare Enter), so "nothing yet" is ``None``, not ``""``.
        """
        if self._box is None:
            self._box = queue.Queue()
            _start_reader(self._source, self._box)
        try:
            item = self._box.get(timeout=_POLL_SLICE_SECONDS)
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            raise item  # a closed/failed stdin reaches the prompt exactly as it did before
        return item

    def _say(self, line: str) -> None:
        # The live ``sys.stdout``: this runs under the update's mirror, so the line lands in
        # ``<root>/logs/update.log`` as it is printed.
        print(line, file=sys.stdout, flush=True)

    # -- the rest of the stream ----------------------------------------------------------------
    def isatty(self) -> bool:
        return self._source.isatty()

    def fileno(self) -> int:
        return self._source.fileno()

    def __getattr__(self, name):
        if name == "_source":  # not yet bound (copy/deepcopy mid-construction): don't recurse
            raise AttributeError(name)
        return getattr(self._source, name)


def _duration(seconds: float) -> str:
    minutes, rest = divmod(int(seconds), 60)
    if not minutes:
        return f"{rest}s"
    return f"{minutes}m" if not rest else f"{minutes}m{rest:02d}s"


def bounded_stdin(source=None, **kwargs) -> BoundedStdin:
    """``sys.stdin`` for the duration of an update run (see the module docstring)."""
    return BoundedStdin(sys.stdin if source is None else source, **kwargs)
