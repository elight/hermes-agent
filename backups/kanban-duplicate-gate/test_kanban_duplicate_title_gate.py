"""Duplicate-title gate: a new card with the identical title of an open card is rejected
unless its description is materially different."""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc

BODY = (
    "Prometheus rule TargetDown is FIRING.\n- host: llmbox / 192.168.1.97:8009\n"
    "- firing since 2026-11-13T11:16:07Z\n- value: 0e+00\nCheck the service, then the firewall.\n"
)


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_identical_title_and_body_rejected(kanban_home):
    with kbc.connect() as conn:
        first = kb.create_task(conn, title="TargetDown: llmbox", body=BODY, assignee="ops")
        with pytest.raises(kb.DuplicateCardError) as e:
            kb.create_task(conn, title="TargetDown: llmbox", body=BODY, assignee="ops")
        assert first in str(e.value) and e.value.existing_id == first
        assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_title_match_ignores_case_and_whitespace(kanban_home):
    with kbc.connect() as conn:
        kb.create_task(conn, title="TargetDown: llmbox", body=BODY, assignee="ops")
        with pytest.raises(kb.DuplicateCardError):
            kb.create_task(conn, title="  targetdown: LLMBOX ", body=BODY, assignee="ops")


def test_only_ids_numbers_and_timestamps_differ_is_still_duplicate(kanban_home):
    other = BODY.replace("8009", "8010").replace("11:16:07", "12:01:44").replace("0e+00", "1e+01")
    with kbc.connect() as conn:
        kb.create_task(conn, title="TargetDown: llmbox", body=BODY, assignee="ops")
        with pytest.raises(kb.DuplicateCardError):
            kb.create_task(conn, title="TargetDown: llmbox", body=other, assignee="ops")


def test_materially_different_body_is_allowed(kanban_home):
    different = (
        "Rotate the Loki ingest key on .37 and update the Alloy config on every host. "
        "Steps: mint a new key, push it, restart Alloy, confirm logs flow, revoke the old key. "
        "Owner is Evan; needs one hands-on session."
    )
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="TargetDown: llmbox", body=BODY, assignee="ops")
        b = kb.create_task(conn, title="TargetDown: llmbox", body=different, assignee="ops")
        assert a != b


def test_different_title_is_never_blocked(kanban_home):
    with kbc.connect() as conn:
        kb.create_task(conn, title="TargetDown: llmbox", body=BODY, assignee="ops")
        kb.create_task(conn, title="TargetDown: pulsarr", body=BODY, assignee="ops")


def test_done_and_archived_cards_do_not_block_a_refile(kanban_home):
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="Disk churn", body=BODY, assignee="ops")
        kb.archive_task(conn, a)
        b = kb.create_task(conn, title="Disk churn", body=BODY, assignee="ops")
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (b,))
        conn.commit()
        kb.create_task(conn, title="Disk churn", body=BODY, assignee="ops")


def test_blocked_and_triage_cards_count_as_open(kanban_home):
    with kbc.connect() as conn:
        kb.create_task(conn, title="Decide X", body=BODY, initial_status="blocked")
        with pytest.raises(kb.DuplicateCardError):
            kb.create_task(conn, title="Decide X", body=BODY)


def test_idempotency_key_hit_still_returns_existing_card(kanban_home):
    with kbc.connect() as conn:
        a = kb.create_task(conn, title="T", body=BODY, assignee="ops", idempotency_key="k1")
        assert kb.create_task(conn, title="T", body=BODY, assignee="ops", idempotency_key="k1") == a


def test_empty_bodies_on_same_title_are_duplicates(kanban_home):
    with kbc.connect() as conn:
        kb.create_task(conn, title="Ping", assignee="ops")
        with pytest.raises(kb.DuplicateCardError):
            kb.create_task(conn, title="Ping", assignee="ops")
