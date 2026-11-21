"""A 40-minute capacity wait must not starve the cron inactivity watchdog (t_627ba2b1).

The cron capacity wait (agent/capacity_wait.py) sleeps through
``agent.turn_recovery.interruptible_backoff_sleep``, whose ~30s ``_touch_activity`` cadence is
what keeps the inactivity watchdog (cron/scheduler_liveness.py, default limit
HERMES_CRON_TIMEOUT=600s — idle time, not wall clock) satisfied. These tests drive a full
40-minute virtual-clock wait under the REAL watchdog loop and prove it never trips; the
control half proves the harness bites when touches stop, so a green main test is not vacuous.
"""
from __future__ import annotations

import threading
from types import SimpleNamespace

from agent.turn_recovery import interruptible_backoff_sleep
from cron.scheduler import _cron_inactivity_seconds
from cron.scheduler_liveness import _inactivity_watchdog_loop

_WAIT_S = 40 * 60  # the longest capacity wait the default budget allows


class _VirtualClock:
    """Thread-shared wall clock advanced by the waiter's sleep slices (no real sleeping)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._now = 0.0

    def time(self) -> float:
        with self._lock:
            return self._now

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds
        # Yield the GIL (event-based, no wall-clock sleep) so the watchdog thread samples.
        threading.Event().wait(0.0)


class _LivenessAgent:
    """The liveness surface ``interruptible_backoff_sleep`` and the watchdog read."""

    _interrupt_requested = False

    def __init__(self, clock: _VirtualClock):
        self._clock = clock
        self._last_activity_ts = clock.time()
        self.touch_count = 0

    def _touch_activity(self, desc, **kwargs):
        self._last_activity_ts = self._clock.time()
        self.touch_count += 1

    def get_activity_summary(self) -> dict:
        return {"seconds_since_activity": self._clock.time() - self._last_activity_ts}

    def idle_seconds(self) -> float:
        return self.get_activity_summary()["seconds_since_activity"]

    def _adopt_nous_key_before_expiry(self):
        return None

    def clear_interrupt(self, **kwargs):
        return False


def _wait_40_minutes_under_watchdog(clock, agent):
    """Run the real interruptible wait (virtual clock) while the real watchdog loop watches.

    Returns (wait_result, tripped)."""
    done = threading.Event()
    outcome = {}

    def _waiter():
        outcome["result"] = interruptible_backoff_sleep(
            agent, _WAIT_S, None, messages=[], conversation_history=None, api_call_count=1,
            abort_message="Interrupt detected during capacity wait, aborting.",
            interrupt_text="Operation interrupted: waiting for the engine slot.",
            activity_label="local-engine capacity wait",
        )
        done.set()

    thread = threading.Thread(target=_waiter, daemon=True)
    thread.start()
    tripped = _inactivity_watchdog_loop(
        get_idle_seconds=agent.idle_seconds, limit_s=600.0, poll_s=0.002,
        stop=threading.Event(), future_done=done.is_set,
    )
    thread.join(timeout=30)
    assert not thread.is_alive(), "capacity wait did not finish on the virtual clock"
    return outcome.get("result"), tripped


def test_forty_minute_capacity_wait_does_not_trip_the_inactivity_watchdog(monkeypatch):
    """The feed to prove: the default 600s idle limit must never bite a fully-slept 40-minute
    capacity wait, because interruptible_backoff_sleep touches activity every ~30s."""
    assert _cron_inactivity_seconds() == 600  # the documented default this test must survive

    clock = _VirtualClock()
    agent = _LivenessAgent(clock)
    # The wait's module-global ``time`` becomes the virtual clock (wall time + sleep only).
    monkeypatch.setattr("agent.turn_recovery.time", SimpleNamespace(time=clock.time, sleep=clock.sleep))

    result, tripped = _wait_40_minutes_under_watchdog(clock, agent)

    assert result is None, "wait should complete, not abort"
    assert tripped is False, "600s inactivity watchdog tripped during an actively-touching 40-minute wait"
    assert clock.time() >= _WAIT_S  # the full 40 minutes really elapsed (virtually)
    assert agent.touch_count >= _WAIT_S // 30 - 1  # ~one touch per 30s of wait


def test_watchdog_harness_bites_when_activity_touches_stop():
    """Control: the same harness with a silent clock-pusher must trip the same watchdog —
    otherwise the passing test above would prove nothing."""
    clock = _VirtualClock()
    agent = _LivenessAgent(clock)  # timestamp frozen at 0: no touches ever happen
    done = threading.Event()

    def _silent_advance():
        # Past the 600s limit and well beyond it, so any polling cadence observes the breach.
        for _ in range((_WAIT_S * 3) * 5):
            clock.sleep(0.2)
        done.set()

    thread = threading.Thread(target=_silent_advance, daemon=True)
    thread.start()
    tripped = _inactivity_watchdog_loop(
        get_idle_seconds=agent.idle_seconds, limit_s=600.0, poll_s=0.002,
        stop=threading.Event(), future_done=done.is_set,
    )
    thread.join(timeout=30)
    assert tripped is True, "watchdog harness is vacuous — it never trips even with no activity"
