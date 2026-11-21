"""Cron-side wiring for the local-engine capacity wait (agent/capacity_wait.py).

The agent sleeps a local engine's "at capacity" hint until a per-run wall-clock budget is
spent. This module resolves that budget when the cron agent is built:

* ``cron.capacity_wait_seconds`` (default 2400, ``0`` disables) is the fleet ceiling;
* the effective per-job budget is ``min(ceiling, seconds until the job's next fire)`` —
  waiting past the next fire is pointless because that fire coalesces as "already running"
  (the in-flight guard in cron/scheduler.py skips it);
* interval jobs use the interval, cron-expression jobs the gap to the next fire (via the
  same timezone-aware ``compute_next_run`` the scheduler advances the slot with), and
  one-shots/unknown schedules take the full ceiling — they have no next fire to coalesce
  with.
"""
from __future__ import annotations

import contextlib
from datetime import datetime
from typing import Any, Optional


def next_fire_gap_seconds(job: dict, *, now: Optional[datetime] = None) -> Optional[float]:
    """Seconds until this job's next fire; None when the schedule has no next fire (one-shot,
    unknown kind) or cannot be resolved (bad persisted shape, missing croniter)."""
    with contextlib.suppress(Exception):
        schedule = job.get("schedule")
        if isinstance(schedule, str) and schedule.strip():
            from cron.jobs import parse_schedule
            schedule = parse_schedule(schedule) or {}
        if not isinstance(schedule, dict):
            return None
        kind = schedule.get("kind")
        if kind == "interval":
            minutes = float(schedule.get("minutes") or 0)
            return minutes * 60.0 if minutes > 0 else None
        if kind == "cron" and schedule.get("expr"):
            # Reuse the scheduler's own timezone-aware next-run computation (job pin >
            # profile zone, DST-safe) rather than re-deriving the wall clock here.
            from cron.jobs import compute_next_run
            from hermes_time import now as hermes_now
            nxt = compute_next_run(schedule, timezone=job.get("timezone"))
            if not nxt:
                return None
            gap = (datetime.fromisoformat(nxt) - (now or hermes_now())).total_seconds()
            return gap if gap > 0 else None
    return None


def capacity_wait_budget_seconds(job: dict, cfg: dict) -> float:
    """Effective per-run wait budget for this job: config ceiling capped by the interval."""
    from agent.capacity_wait import capacity_wait_config_seconds
    budget = capacity_wait_config_seconds(cfg)
    if budget <= 0:
        return 0.0
    gap = next_fire_gap_seconds(job)
    return min(budget, gap) if gap is not None else budget


def attach_capacity_wait_budget(agent: Any, job: dict, cfg: dict) -> None:
    """Stamp the resolved budget and the job label on a freshly built cron agent; the agent
    side (``agent/capacity_wait.py``) reads exactly these two attributes."""
    agent._cron_capacity_wait_budget_s = capacity_wait_budget_seconds(job, cfg)
    name, job_id = str(job.get("name") or ""), str(job.get("id") or "")
    agent._cron_capacity_wait_job_label = f"{name} ({job_id})" if name and job_id else (name or job_id)
