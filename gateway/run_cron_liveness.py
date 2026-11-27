"""Cron-silence visibility: the gateway says out loud what a wedged ticker cannot.

Background (wiki: cron-tick-silent-stall-2026-11-20): cron went silent host-wide twice and the
gateway logged NOTHING. The ops home lost ~63 h to a ``gateway.parked`` marker quietly removing it
from the tick roster; default and principal-engineer lost ~6 h to a profile-gate stand-down that
survived five gateway processes with zero lines. The ticker's own guards can only speak from inside
the thread that is wedged, so every announce here is driven either from the housekeeping thread (a
different thread, reading the ``cron/ticker_heartbeat`` marker) or from the ticker's own admission
gate:

- ``CronLivenessAnnouncer.observe_heartbeat`` — one ``error`` per episode when a served home's
  heartbeat ages past ``CRON_SILENT_AFTER_S``, one recovery line when beats return.
- ``CronLivenessAnnouncer.note_gate`` — the profile gate stood down silently by design; now one
  INFO per (home, episode) names the liveness rung and rival pid behind the stand-down.
- ``CronLivenessAnnouncer.note_roster`` — park/standalone roster exits announced once per
  (profile, episode), and the return too.
- ``CronLivenessAnnouncer.maybe_arm_dump_timer`` — at ~10 min of silence, arm
  ``faulthandler.dump_traceback_later`` (all threads, every minute, to ``gateway_faulthandler.log``)
  and cancel on fresh beats. The blocked line is the one thing a wedged ticker can never report.

Every announce is log-once-per-episode (same discipline as
``cron.scheduler_tick._log_tick_yield_once``): repeating the line every housekeeping tick is noise,
and never logging it is the November incident.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, TextIO

logger = logging.getLogger(__name__)

# Heartbeat age past which a home is announced silent. Matches the external watchdog threshold
# (shared-scripts/cron_liveness_watch.py) so both surfaces agree on what "stalled" means.
CRON_SILENT_AFTER_S = 900.0
# Traceback dumps start one rung EARLIER than the error line: by the time the silence is announced,
# the stacks should already be on disk (Nov-20 could not name the blocked line for lack of them).
CRON_FAULTHANDLER_ARM_AFTER_S = 600.0
CRON_FAULTHANDLER_DUMP_EVERY_S = 60.0
# A boot inherits markers written by its predecessor, so silence measured against a pre-boot beat
# would cry wolf on every restart. Announce only once this process has had many ticker cycles to
# beat; a ticker wedged from boot is announced at boot + grace (still far under the ~25 min the
# external watchdog takes).
STARTUP_GRACE_S = 900.0
# A gated home whose last gate rejection is older than this has not been asked about since — the
# ticker loop is no longer reaching the gate — so it leaves the gated snapshot. Two ticker cycles.
_GATE_STATE_STALE_S = 180.0


class _AnnounceLog(Protocol):
    """What the announcer needs from a logger; tests pass a recording stub."""

    def error(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def warning(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def info(self, msg: str, *args: object, **kwargs: object) -> None: ...

    def debug(self, msg: str, *args: object, **kwargs: object) -> None: ...


class _DumpTimer(Protocol):
    """Arm/cancel surface for the all-thread traceback dump (real or test double)."""

    dump_every_s: float

    def arm(self, open_log: Callable[[], TextIO]) -> None: ...

    def cancel(self) -> None: ...


class _ProcessFaulthandler:
    """Real ``faulthandler`` dump timer. Only one dump-later call may be pending process-wide, so
    this is owned by the single default announcer; the file handle stays open across arm/cancel."""

    def __init__(self, dump_every_s: float = CRON_FAULTHANDLER_DUMP_EVERY_S) -> None:
        self.dump_every_s = dump_every_s
        self._fh: Optional[TextIO] = None

    def arm(self, open_log: Callable[[], TextIO]) -> None:
        import faulthandler

        if self._fh is None:
            self._fh = open_log()
        faulthandler.dump_traceback_later(self.dump_every_s, repeat=True, file=self._fh)

    def cancel(self) -> None:
        import faulthandler

        cancel = getattr(faulthandler, "cancel_dump_traceback_later", None)
        if cancel is not None:
            cancel()


class CronLivenessAnnouncer:
    """Log-once-per-episode state for ticker heartbeats, gate stand-downs, roster exits and the
    traceback dump timer. Episode dicts are keyed by profile name and live for the process.

    Thread-safety: ``note_gate`` runs on the ticker thread; everything else runs on the
    housekeeping thread. One lock covers all episode state (announces are rare; contention moot).
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        silent_after_s: float = CRON_SILENT_AFTER_S,
        arm_after_s: float = CRON_FAULTHANDLER_ARM_AFTER_S,
        grace_s: float = STARTUP_GRACE_S,
        faulthandler_ops: Optional[_DumpTimer] = None,
        log: Optional[_AnnounceLog] = None,
    ) -> None:
        self._now = monotonic
        self._silent_after_s = silent_after_s
        self._arm_after_s = arm_after_s
        self._grace_s = grace_s
        self._fd = faulthandler_ops if faulthandler_ops is not None else _ProcessFaulthandler()
        self._log = log if log is not None else logger
        self._lock = threading.Lock()
        self._boot = monotonic()
        self._silent: dict[str, dict] = {}
        self._gate: dict[str, dict] = {}
        self._gate_last_call: Optional[float] = None
        self._roster_out: dict[str, dict] = {}
        self._dump_armed = False
        self._dump_since = 0.0

    # ── heartbeat episodes ────────────────────────────────────────────────────────────────────

    def observe_heartbeat(self, name: str, age_s: Optional[float], context: str) -> None:
        """Announce one home's ticker-heartbeat silence/recovery, once per episode.

        ``age_s`` None means "no readable marker" — that is UNKNOWN (a profile created minutes ago
        has never beaten), not silent; the gate line's own evidence carries the stall stories.
        """
        with self._lock:
            episode = self._silent.get(name)
            if age_s is None:
                return
            if age_s > self._silent_after_s:
                if episode is None:
                    if self._now() - self._boot < self._grace_s:
                        return  # marker predates this process; the ticker has not had its cycles yet
                    self._silent[name] = {"since": self._now()}
                    self._log.error(
                        "CRON TICK SILENT: profile %s last beat %ds ago — %s",
                        name, int(age_s), context)
                return
            if episode is not None:
                del self._silent[name]
                self._log.warning(
                    "CRON TICK RESUMED: profile %s heartbeat fresh again after %ds of silence",
                    name, int(self._now() - episode["since"]))

    # ── profile-gate episodes ─────────────────────────────────────────────────────────────────

    def note_gate(self, name: str, rejected: bool, source: str = "", rival_pid: Optional[int] = None) -> None:
        """One INFO per (home, episode) when the cron profile gate stands this process down for a
        rival gateway (and one when the home comes back). The Nov-20 host-wide stand-down was
        invisible because the gate answered silently; ``source`` is the liveness rung that answered
        (pid/health/runtime_status/multiplexer)."""
        with self._lock:
            self._gate_last_call = self._now()
            episode = self._gate.get(name)
            if rejected:
                if episode is None:
                    self._gate[name] = {"since": self._now(), "rival": rival_pid}
                    self._log.info(
                        "Cron profile gate: this process stands down on %s — another gateway owns "
                        "it (pid %s, liveness rung %s); its jobs tick from that process",
                        name, rival_pid, source or "unknown")
                else:
                    episode["at"] = self._now()
                    episode["rival"] = rival_pid
                return
            if episode is not None:
                del self._gate[name]
                self._log.info(
                    "Cron profile gate: %s is this process's again after %ds of stand-down "
                    "(rival pid %s no longer answers); ticking it here",
                    name, int(self._now() - episode["since"]), episode["rival"])

    def gate_snapshot(self) -> tuple[list[str], Optional[float]]:
        """(currently-gated profile names, seconds since the last gate call) for announce context.
        A gated home whose last rejection predates ``_GATE_STATE_STALE_S`` has left the set: the
        ticker stopped consulting the gate entirely, which the call age itself reveals."""
        with self._lock:
            now = self._now()
            names = sorted(
                name for name, ep in self._gate.items()
                if now - ep.get("at", ep["since"]) <= _GATE_STATE_STALE_S)
            call_age = None if self._gate_last_call is None else now - self._gate_last_call
            return names, call_age

    # ── roster episodes (park / standalone exits) ─────────────────────────────────────────────

    def note_roster(self, served_names: set[str], dropped: dict[str, str]) -> None:
        """Announce profiles the tick roster dropped (``gateway.parked`` / ``gateway.standalone``)
        once per (profile, episode), and their return. The ops home's 63 h silence began exactly
        this way: a parked marker removed it from the roster and nothing ever said so."""
        with self._lock:
            for name, reason in sorted(dropped.items()):
                if name in self._roster_out:
                    continue
                self._roster_out[name] = {"since": self._now()}
                if reason == "gateway.parked":
                    self._log.info(
                        "Cron roster: profile %s left the tick roster (gateway.parked) — no gateway "
                        "on this host ticks its jobs until it is un-parked", name)
                elif reason == "gateway.standalone":
                    self._log.info(
                        "Cron roster: profile %s left the tick roster (gateway.standalone) — only "
                        "its own gateway process ticks it (if one is running)", name)
                else:
                    self._log.info(
                        "Cron roster: profile %s is not in the tick roster (%s)", name, reason)
            for name in sorted(self._roster_out):
                if name in dropped:
                    continue
                episode = self._roster_out.pop(name)
                if name in served_names:
                    self._log.info(
                        "Cron roster: profile %s rejoined the tick roster after %ds out",
                        name, int(self._now() - episode["since"]))

    # ── faulthandler arming ───────────────────────────────────────────────────────────────────

    def maybe_arm_dump_timer(self, worst_age_s: Optional[float], open_log: Callable[[], TextIO]) -> None:
        """Arm all-thread traceback dumps while any home's heartbeat crosses ``arm_after_s``;
        cancel on fresh beats. The armed flag matters twice over: only one dump-later may be
        pending process-wide, and re-arming every housekeeping tick would push each dump further out."""
        with self._lock:
            if self._now() - self._boot < self._grace_s:
                return
            if worst_age_s is not None and worst_age_s > self._arm_after_s:
                if self._dump_armed:
                    return
                try:
                    self._fd.arm(open_log)
                except Exception:
                    self._log.debug(
                        "could not arm cron-stall traceback dumps", exc_info=True)
                    return
                self._dump_armed = True
                self._dump_since = self._now()
                self._log.warning(
                    "Cron heartbeat silent %ds (arm threshold %ds): dumping all-thread tracebacks to "
                    "gateway_faulthandler.log every %ds until beats return",
                    int(worst_age_s), int(self._arm_after_s), int(self._fd.dump_every_s))
                return
            if self._dump_armed:
                self._dump_armed = False
                armed_for = int(self._now() - self._dump_since)
                try:
                    self._fd.cancel()
                except Exception:
                    self._log.debug(
                        "could not cancel cron-stall traceback dumps", exc_info=True)
                self._log.info(
                    "Cron beats healthy again: traceback dump timer disarmed after %ds armed", armed_for)


def open_faulthandler_log() -> TextIO:
    """Append-mode ``<launch home>/logs/gateway_faulthandler.log`` (mirrors
    ``GatewayRunner._open_faulthandler_log`` for callers without the runner; the launch home is
    where gateway.log itself lands, so the dumps sit beside the announce lines)."""
    from hermes_constants import get_hermes_home

    log_dir = Path(str(get_hermes_home())) / "logs"
    os.makedirs(log_dir, exist_ok=True)
    return open(os.path.join(str(log_dir), "gateway_faulthandler.log"), "a", encoding="utf-8")  # windows-footgun: ok (append log writer, not a read)


default_announcer = CronLivenessAnnouncer()


def note_cron_gate(name: str, rejected: bool, source: str = "", rival_pid: Optional[int] = None) -> None:
    """Gate hook used by ``gateway.run._cron_profile_gate``. Never raises: an announce must not
    flip a tick-admission outcome (the caller's ``return not rejected`` has to run)."""
    try:
        default_announcer.note_gate(name, rejected, source=source, rival_pid=rival_pid)
    except Exception:
        logger.debug("cron gate announce failed for %s", name, exc_info=True)


def read_home_heartbeat(home: Path) -> tuple[Optional[float], str]:
    """(heartbeat age, pid description) for one cron home, read under an explicit store binding
    (``use_cron_store`` — a housekeeping thread carries no profile scope). Never raises:
    ``age=None`` is "unknown", pid desc says what the marker did not."""
    from cron.jobs import (
        get_ticker_heartbeat_age,
        get_ticker_heartbeat_writer_pid,
        ticker_heartbeat_writer_alive,
        use_cron_store,
    )

    try:
        with use_cron_store(home):
            age = get_ticker_heartbeat_age()
            pid = get_ticker_heartbeat_writer_pid()
            alive = ticker_heartbeat_writer_alive() if pid is not None else False
    except Exception:
        return None, "(pid unreadable)"
    if pid is None:
        return age, "(pid unknown)"
    return age, f"(pid {pid} {'alive' if alive else 'dead'})"


def cron_liveness_chore(runner: Any, cron_thread: Any, *, announcer: Optional[CronLivenessAnnouncer] = None) -> None:
    """One housekeeping pass (60 s cadence): announce silent homes, park/standalone roster exits,
    and arm/cancel traceback dumps. Runs on the HOUSEKEEPING thread so it reports even when the
    ticker thread itself is wedged — that is the whole point (Nov-20: ticker frozen, housekeeping
    healthy, log silent)."""
    ann = announcer if announcer is not None else default_announcer
    from gateway.run import _cron_tick_profile_homes

    try:
        homes = _cron_tick_profile_homes(getattr(runner, "config", None))
    except Exception:
        logger.debug("cron liveness chore: tick-roster read failed", exc_info=True)
        return

    gated, gate_call_age = ann.gate_snapshot()
    ticker_alive = cron_thread.is_alive() if cron_thread is not None else None
    gated_desc = f"gated={len(gated)}"
    if gated:
        gated_desc += f" [{', '.join(gated)}]"
    if gate_call_age is None:
        gated_desc += " (gate never called by this process)"
    else:
        gated_desc += f" (gate last called {int(gate_call_age)}s ago)"

    worst_age: Optional[float] = None
    for name, home in homes:
        age, pid_desc = read_home_heartbeat(home)
        if age is not None and (worst_age is None or age > worst_age):
            worst_age = age
        ann.observe_heartbeat(
            name, age,
            f"{pid_desc} — ticker thread alive={ticker_alive}, {gated_desc}")

    ann.maybe_arm_dump_timer(
        worst_age, getattr(runner, "_open_faulthandler_log", None) or open_faulthandler_log)
    _note_roster_exits(ann, homes)


def _note_roster_exits(ann: CronLivenessAnnouncer, homes: list[tuple[str, Path]]) -> None:
    """Diff the tick roster against ALL installed profiles (the set the multiplexer would serve
    without markers) and hand the exits to the announcer."""
    from hermes_cli.profiles import profile_is_parked, profile_is_standalone, profiles_to_serve

    try:
        installed = profiles_to_serve(True, include_standalone=True, include_parked=True)
    except Exception:
        logger.debug("cron liveness chore: installed-profile read failed", exc_info=True)
        return
    served = {name for name, _home in homes}
    dropped: dict[str, str] = {}
    for name, home in installed:
        # The default home is never really dropped (profiles_to_serve warns and serves it).
        if name in served or name == "default":
            continue
        try:
            if profile_is_parked(home):
                dropped[name] = "gateway.parked"
            elif profile_is_standalone(home):
                dropped[name] = "gateway.standalone"
            else:
                dropped[name] = "absent from the multiplex roster"
        except Exception:
            continue
    ann.note_roster(served, dropped)
