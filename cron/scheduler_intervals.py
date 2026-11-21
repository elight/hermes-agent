"""Job schedule cadence helpers for the scheduler's in-flight guard.

Pure read-only arithmetic over a job's persisted ``schedule`` dict: how far apart the job's
fires are, in minutes. Used to size the stale in-flight allowance (a job's claim must not be
force-released before a reasonable multiple of its own cadence). Kept out of scheduler.py so
the tick-loop module stays focused on dispatch.
"""
from __future__ import annotations

import contextlib
from typing import Optional

# expr -> minutes; cadence never changes, so avoid re-evaluating croniter every tick.
_cron_interval_cache: dict = {}


def _cron_interval_minutes(expr: str) -> Optional[float]:
    """Cron expression cadence (gap between next two fires) in minutes; None -> floor allowance."""
    if expr in _cron_interval_cache:
        return _cron_interval_cache[expr]
    result = None
    ok = False
    with contextlib.suppress(Exception):
        from cron.jobs import _ensure_croniter

        ok = _ensure_croniter()
        if ok:
            from cron.jobs import croniter as _croniter
            from datetime import datetime

            base = datetime.now()
            it = _croniter(expr, base)
            first = it.get_next(datetime)
            second = it.get_next(datetime)
            gap = (second - first).total_seconds() / 60.0
            result = gap if gap > 0 else None
    # Cache a real cadence or a bad-expr None (croniter loaded, expr invalid: stable). Skip the
    # None from a transient croniter ImportError so it can't pin the floor allowance for the
    # process lifetime once the import recovers.
    if result is not None or ok:
        _cron_interval_cache[expr] = result
    return result


def _job_interval_minutes(job: dict) -> Optional[float]:
    """Best-effort job interval in minutes (None if unknown / one-shot -> floor). ``schedule`` is
    persisted as a parsed dict; the string path is only a fallback for programmatic callers."""
    with contextlib.suppress(Exception):
        schedule = job.get("schedule")
        if isinstance(schedule, str) and schedule.strip():
            from cron.jobs import parse_schedule

            schedule = parse_schedule(schedule) or {}
        if isinstance(schedule, dict):
            kind = schedule.get("kind")
            if kind == "interval":
                minutes = schedule.get("minutes")
                return float(minutes) if minutes else None
            if kind == "cron":
                return _cron_interval_minutes(str(schedule.get("expr") or ""))
    return None
