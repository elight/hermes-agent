"""Episode (log-once) logic for the cron-silence announces (gateway/run_cron_liveness.py).

These pin the behaviour that was MISSING during the 2026-11-20 host-wide stall: silence that
crosses the threshold must produce EXACTLY ONE line per episode (and one recovery line), the
startup grace must not cry wolf on inherited markers, and the traceback dump timer must arm once
and cancel on fresh beats. The chore test exercises the housekeeping wiring end to end with the
filesystem seams stubbed.
"""
from __future__ import annotations

import logging
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import gateway.run as run_mod
import gateway.status as status_mod
from hermes_cli import profiles
from gateway import run_cron_liveness as rcl
from gateway.run_cron_liveness import CronLivenessAnnouncer


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _Log:
    """Recording logger stub matching the announcer's _AnnounceLog protocol."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _rec(self, level: str, msg: str, *args: object) -> None:
        self.lines.append((level, msg % args if args else msg))

    def error(self, msg: str, *args: object, **kw: object) -> None:
        self._rec("error", msg, *args)

    def warning(self, msg: str, *args: object, **kw: object) -> None:
        self._rec("warning", msg, *args)

    def info(self, msg: str, *args: object, **kw: object) -> None:
        self._rec("info", msg, *args)

    def debug(self, msg: str, *args: object, **kw: object) -> None:
        self._rec("debug", msg, *args)

    def levels(self, needle: str) -> list[str]:
        return [lvl for lvl, text in self.lines if needle in text]

    def texts(self, needle: str) -> list[str]:
        return [text for _lvl, text in self.lines if needle in text]


class _FakeDumper:
    dump_every_s = 60.0

    def __init__(self) -> None:
        self.arms = 0
        self.cancels = 0

    def arm(self, open_log) -> None:
        self.arms += 1

    def cancel(self) -> None:
        self.cancels += 1


def _announcer(clock: _Clock, log: _Log, dumper: _FakeDumper, *, grace_s: float = 0.0):
    return CronLivenessAnnouncer(
        monotonic=clock, grace_s=grace_s, faulthandler_ops=dumper, log=log)


# ── heartbeat episodes ─────────────────────────────────────────────────────────────────────────

def test_silence_announces_exactly_once_per_episode() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)

    for _ in range(5):  # five housekeeping passes inside one episode
        ann.observe_heartbeat("ops", 960.0, "(pid 7 alive) — ctx")
    assert log.levels("CRON TICK SILENT") == ["error"]
    line = log.texts("CRON TICK SILENT")[0]
    assert "profile ops" in line and "960s ago" in line and "(pid 7 alive)" in line
    assert log.levels("CRON TICK RESUMED") == []

    ann.observe_heartbeat("ops", 30.0, "ctx")  # recovery
    assert log.levels("CRON TICK RESUMED") == ["warning"]
    ann.observe_heartbeat("ops", 45.0, "ctx")  # still fine: no repeat line
    assert len(log.texts("CRON TICK RESUMED")) == 1


def test_second_silence_episode_announces_again() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)

    ann.observe_heartbeat("ops", 1000.0, "ctx")
    ann.observe_heartbeat("ops", 10.0, "ctx")
    ann.observe_heartbeat("ops", 20.0, "ctx")
    ann.observe_heartbeat("ops", 1200.0, "ctx")  # new episode after recovery
    assert len(log.texts("CRON TICK SILENT")) == 2


def test_missing_marker_is_unknown_not_silent() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)
    for _ in range(3):
        ann.observe_heartbeat("fresh-profile", None, "ctx")
    assert log.lines == []


def test_startup_grace_suppresses_inherited_staleness_then_expires() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper, grace_s=900.0)

    for _ in range(3):  # a restart sees its dead predecessor's 8-hour-old marker
        ann.observe_heartbeat("default", 28800.0, "ctx")
    assert log.lines == []
    clock.advance(901.0)
    ann.observe_heartbeat("default", 28800.0, "ctx")  # grace over, still never beat here
    assert log.levels("CRON TICK SILENT") == ["error"]


def test_episodes_are_per_home() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)
    ann.observe_heartbeat("ops", 960.0, "ctx")
    ann.observe_heartbeat("default", 960.0, "ctx")
    assert len(log.texts("CRON TICK SILENT")) == 2
    ann.observe_heartbeat("ops", 5.0, "ctx")
    assert len(log.texts("CRON TICK SILENT")) == 2  # ops recovery must not touch default's episode


# ── gate episodes ──────────────────────────────────────────────────────────────────────────────

def test_gate_stand_down_logs_once_per_episode_with_rung_and_pid() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)

    for _ in range(4):  # one ticker cycle per call, all rejected
        ann.note_gate("beta", True, source="health", rival_pid=4242)
    assert log.levels("stands down on beta") == ["info"]
    line = log.texts("stands down on beta")[0]
    assert "pid 4242" in line and "health" in line

    ann.note_gate("beta", False, source="pid", rival_pid=None)  # rival gone
    assert log.levels("is this process's again") == ["info"]
    ann.note_gate("beta", True, source="pid", rival_pid=99)  # new episode announces again
    assert len(log.texts("stands down on beta")) == 2


def test_gate_snapshot_tracks_gated_homes_and_call_age() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)

    names, call_age = ann.gate_snapshot()
    assert names == [] and call_age is None
    ann.note_gate("beta", True, source="pid", rival_pid=7)
    names, call_age = ann.gate_snapshot()
    assert names == ["beta"] and call_age == 0.0
    clock.advance(200.0)  # ticker stopped consulting the gate: entry ages out of the set
    names, call_age = ann.gate_snapshot()
    assert names == [] and call_age == 200.0


# ── roster episodes ────────────────────────────────────────────────────────────────────────────

def test_roster_park_and_unpark_announce_once_each() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)

    ann.note_roster({"default"}, {"ops": "gateway.parked"})
    ann.note_roster({"default"}, {"ops": "gateway.parked"})  # still parked: no repeat
    assert log.levels("left the tick roster") == ["info"]
    assert "no gateway on this host ticks its jobs" in log.texts("left the tick roster")[0]

    ann.note_roster({"default", "ops"}, {})
    assert log.levels("rejoined the tick roster") == ["info"]
    ann.note_roster({"default", "ops"}, {})  # back: no repeat
    assert len(log.texts("rejoined the tick roster")) == 1


def test_standalone_drop_names_the_own_gateway_consequence() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)
    ann.note_roster({"default"}, {"pe": "gateway.standalone"})
    assert "its own gateway process ticks it" in log.texts("left the tick roster")[0]


def test_deleted_profile_leaves_no_rejoin_line() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)
    ann.note_roster({"default"}, {"gone": "gateway.parked"})
    ann.note_roster({"default"}, {})  # vanished from installed set entirely: silently forgotten
    assert log.levels("rejoined the tick roster") == []


# ── traceback dump timer ───────────────────────────────────────────────────────────────────────

def test_dump_timer_arms_once_and_cancels_on_fresh_beats() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)

    for _ in range(3):
        ann.maybe_arm_dump_timer(700.0, StringIO)
    assert dumper.arms == 1 and dumper.cancels == 0
    assert log.levels("dumping all-thread tracebacks") == ["warning"]

    for _ in range(2):
        ann.maybe_arm_dump_timer(10.0, StringIO)
    assert dumper.arms == 1 and dumper.cancels == 1  # cancel exactly once
    assert log.levels("disarmed") == ["info"]

    ann.maybe_arm_dump_timer(650.0, StringIO)  # new stall: re-arms
    assert dumper.arms == 2


def test_dump_timer_honours_startup_grace() -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper, grace_s=900.0)
    ann.maybe_arm_dump_timer(28800.0, StringIO)  # predecessor's marker, boot too recent
    assert dumper.arms == 0 and log.lines == []


def test_dump_timer_arm_failure_never_raises() -> None:
    class _Boom(_FakeDumper):
        def arm(self, open_log) -> None:
            raise OSError("disk gone")

    clock, log = _Clock(), _Log()
    ann = _announcer(clock, log, _Boom())
    ann.maybe_arm_dump_timer(700.0, StringIO)  # must not propagate
    assert log.levels("dumping all-thread tracebacks") == []


# ── chore wiring (housekeeping seam) ───────────────────────────────────────────────────────────

def test_chore_announces_silent_home_and_arms_dumps(monkeypatch, tmp_path) -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)
    homes = [("ops", tmp_path / "ops"), ("default", tmp_path)]
    monkeypatch.setattr(run_mod, "_cron_tick_profile_homes", lambda _cfg: list(homes))
    ages = {tmp_path / "ops": (1000.0, "(pid 11 dead)"), tmp_path: (5.0, "(pid 11 alive)")}
    monkeypatch.setattr(
        rcl, "read_home_heartbeat", lambda home: ages[home])
    monkeypatch.setattr(
        profiles, "profiles_to_serve",
        lambda _mx, **kw: [("ops", tmp_path / "ops"), ("default", tmp_path)])

    runner = SimpleNamespace(config=None)
    ticker = SimpleNamespace(is_alive=lambda: False)
    rcl.cron_liveness_chore(runner, ticker, announcer=ann)

    silent = log.texts("CRON TICK SILENT")
    assert len(silent) == 1 and "profile ops" in silent[0]
    assert "ticker thread alive=False" in silent[0]
    assert dumper.arms == 1  # worst home crossed the arm threshold
    assert log.levels("left the tick roster") == []  # nothing dropped from the roster


def test_chore_announces_parked_roster_exit(monkeypatch, tmp_path) -> None:
    clock, log, dumper = _Clock(), _Log(), _FakeDumper()
    ann = _announcer(clock, log, dumper)
    monkeypatch.setattr(
        run_mod, "_cron_tick_profile_homes", lambda _cfg: [("default", tmp_path)])
    monkeypatch.setattr(rcl, "read_home_heartbeat", lambda home: (5.0, "(pid 1 alive)"))
    parked_home = tmp_path / "ops"
    monkeypatch.setattr(
        profiles, "profiles_to_serve",
        lambda _mx, **kw: [("default", tmp_path), ("ops", parked_home)])
    monkeypatch.setattr(profiles, "profile_is_parked", lambda home: home is parked_home)

    rcl.cron_liveness_chore(SimpleNamespace(config=None), None, announcer=ann)

    assert log.levels("left the tick roster") == ["info"]
    assert "gateway.parked" in log.texts("left the tick roster")[0]
    assert dumper.arms == 0  # fresh beats: no dump


def test_gate_hook_is_wired_into_run_gate(monkeypatch) -> None:
    """gateway.run._cron_profile_gate must speak through note_cron_gate (one line per episode)."""
    class _Liveness:
        running = True
        pid = 4321
        source = "multiplexer"

    monkeypatch.setattr(status_mod, "resolve_gateway_liveness",
                        lambda **kw: _Liveness(), raising=False)
    monkeypatch.setattr(status_mod, "get_running_pid", lambda *a, **kw: 4321)

    rec = _Log()
    fresh = CronLivenessAnnouncer(monotonic=_Clock(), grace_s=0.0,
                                  faulthandler_ops=_FakeDumper(), log=rec)
    monkeypatch.setattr(rcl, "default_announcer", fresh)

    assert run_mod._cron_profile_gate("wired-test", Path("/nonexistent-home")) is False
    assert run_mod._cron_profile_gate("wired-test", Path("/nonexistent-home")) is False
    assert len(rec.texts("stands down on wired-test")) == 1
    assert "pid 4321" in rec.texts("stands down on wired-test")[0]
    assert "multiplexer" in rec.texts("stands down on wired-test")[0]


def test_note_cron_gate_never_raises(monkeypatch) -> None:
    class _Boom(CronLivenessAnnouncer):
        def note_gate(self, *a, **kw):
            raise RuntimeError("announcer exploded")

    monkeypatch.setattr(rcl, "default_announcer", _Boom())
    rcl.note_cron_gate("x", True, source="pid", rival_pid=1)  # must not propagate


def test_module_logger_name_stable_for_filters() -> None:
    # gateway.log routing keys off module loggers; keep the name part of the contract.
    assert rcl.logger.name == "gateway.run_cron_liveness"
    assert isinstance(rcl.logger, logging.Logger)
