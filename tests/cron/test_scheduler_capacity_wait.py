"""Per-job capacity-wait budget for cron runs (t_627ba2b1).

The agent-side capacity wait (agent/capacity_wait.py) sleeps on a local engine's
"at capacity" hint until its wall-clock budget is spent. The cron scheduler resolves the
effective budget per job: ``cron.capacity_wait_seconds`` (default 2400, 0 disables), capped
by the job's schedule interval — waiting past the next fire is pointless because that fire
coalesces as "already running". Interval jobs use the interval; cron-expression jobs use the
gap to the next fire; one-shots/unknown schedules take the full configured budget (no next
fire to coalesce with).
"""
from __future__ import annotations

import pytest

from datetime import timedelta

from agent.capacity_wait import DEFAULT_CAPACITY_WAIT_SECONDS
from cron import jobs
from cron.scheduler_capacity_wait import attach_capacity_wait_budget, next_fire_gap_seconds
from cron.scheduler_capacity_wait import capacity_wait_budget_seconds


# ── interval jobs: min(config, interval) ──────────────────────────────────────────

def test_ten_minute_interval_caps_the_default_budget_to_its_interval():
    job = {"schedule": {"kind": "interval", "minutes": 10}}
    assert capacity_wait_budget_seconds(job, {}) == 600.0


def test_hourly_interval_leaves_the_default_budget_untouched():
    job = {"schedule": {"kind": "interval", "minutes": 60}}
    assert capacity_wait_budget_seconds(job, {}) == DEFAULT_CAPACITY_WAIT_SECONDS


def test_config_below_the_interval_wins():
    job = {"schedule": {"kind": "interval", "minutes": 10}}
    cfg = {"cron": {"capacity_wait_seconds": 300}}
    assert capacity_wait_budget_seconds(job, cfg) == 300.0


def test_zero_config_disables_even_with_an_interval():
    job = {"schedule": {"kind": "interval", "minutes": 10}}
    assert capacity_wait_budget_seconds(job, {"cron": {"capacity_wait_seconds": 0}}) == 0.0


def test_string_schedule_is_parsed_like_a_dict_schedule():
    assert capacity_wait_budget_seconds({"schedule": "10m"}, {}) == 600.0


# ── cron-expression jobs: gap to the next fire ─────────────────────────────────────

def test_cron_expression_job_uses_the_gap_to_next_fire(monkeypatch):
    now = jobs._hermes_now()
    monkeypatch.setattr(jobs, "compute_next_run",
                        lambda schedule, last_run_at=None, *, timezone=None: (now + timedelta(seconds=90)).isoformat())
    job = {"schedule": {"kind": "cron", "expr": "*/10 * * * *"}}
    assert capacity_wait_budget_seconds(job, {}) == pytest.approx(90.0, abs=0.5)  # min(2400, 90s to next fire)


def test_cron_expression_budget_never_exceeds_the_cadence():
    """Real next-fire computation: a */10 job's budget must sit inside (0, 600] — the
    distance to the NEXT fire, never the 2400s default."""
    job = {"schedule": {"kind": "cron", "expr": "*/10 * * * *"}}
    budget = capacity_wait_budget_seconds(job, {})
    assert 0 < budget <= 600.0


def test_cron_expression_job_past_its_next_fire_falls_back_to_config(monkeypatch):
    now = jobs._hermes_now()
    monkeypatch.setattr(jobs, "compute_next_run",
                        lambda schedule, last_run_at=None, *, timezone=None: (now - timedelta(seconds=5)).isoformat())
    job = {"schedule": {"kind": "cron", "expr": "0 9 * * *"}}
    assert capacity_wait_budget_seconds(job, {}) == DEFAULT_CAPACITY_WAIT_SECONDS


# ── one-shots / unknown shapes: full config budget, never a crash ─────────────────

def test_oneshot_and_unknown_schedules_take_the_full_config_budget():
    assert capacity_wait_budget_seconds({"schedule": {"kind": "once", "run_at": "2030-01-01T09:00:00+00:00"}}, {}) == DEFAULT_CAPACITY_WAIT_SECONDS
    assert capacity_wait_budget_seconds({"schedule": {"kind": "bogus"}}, {}) == DEFAULT_CAPACITY_WAIT_SECONDS
    assert capacity_wait_budget_seconds({}, {}) == DEFAULT_CAPACITY_WAIT_SECONDS
    assert capacity_wait_budget_seconds({"schedule": "not a schedule"}, {}) == DEFAULT_CAPACITY_WAIT_SECONDS


def test_next_fire_gap_reports_interval_and_none_when_unknown():
    assert next_fire_gap_seconds({"schedule": {"kind": "interval", "minutes": 30}}) == 1800.0
    assert next_fire_gap_seconds({"schedule": {"kind": "once", "run_at": "2030-01-01T09:00:00+00:00"}}) is None
    assert next_fire_gap_seconds({"schedule": {"kind": "cron"}}) is None  # expr missing


# ── attach: what the scheduler leaves on the agent ────────────────────────────────

class _Agent:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_attach_sets_job_capped_budget_and_label():
    agent = _Agent()
    job = {"id": "job_pat1", "name": "llmbox-pr-patrol", "schedule": {"kind": "interval", "minutes": 10}}
    attach_capacity_wait_budget(agent, job, {"cron": {"capacity_wait_seconds": 2400}})
    assert agent._cron_capacity_wait_budget_s == 600.0
    assert agent._cron_capacity_wait_job_label == "llmbox-pr-patrol (job_pat1)"


def test_attach_disabled_config_lands_a_zero_budget():
    agent = _Agent()
    job = {"id": "j", "schedule": {"kind": "interval", "minutes": 5}}
    attach_capacity_wait_budget(agent, job, {"cron": {"capacity_wait_seconds": 0}})
    assert agent._cron_capacity_wait_budget_s == 0.0
