"""The kanban duplicate-card gate, ported to the ``kanban-duplicate-gate`` plugin.

The nine tests of the working-tree gate that ran uncommitted inside
``hermes_cli/kanban_db.py`` (card ``t_92f2a27c``, ``test_kanban_duplicate_title_gate.py``),
one per rule, now driving the real dispatch path instead of calling
``kanban_db.create_task`` directly::

    model_tools.handle_function_call("kanban_create", args)
        -> pre_tool_call plugin hook        <- kanban-duplicate-gate refuses here
        -> tools.registry.dispatch -> tools/kanban_tools._handle_create
        -> kanban_db.create_task -> the board

What changed in the port, and only this: a refusal at the tool boundary is a
``{"error": "duplicate card: ..."}`` tool result with no row written, not a
raised ``kanban_db.DuplicateCardError`` (a hook can only return a directive).
The first test asserts the gate's own ``DuplicateCardError`` and its
``existing_id`` directly, so the original assertion survives; every other test
asserts the refusal text and the untouched board.

GREEN — the plugin is installed into a temp home and loaded by real discovery::

    scripts/run_tests.sh tests/plugins/test_kanban_duplicate_title_gate.py

RED — the same file with the plugin's directory renamed aside, so nothing
subscribes to ``pre_tool_call`` and every duplicate is created::

    mv local-plugins/kanban-duplicate-gate{,.off}
    scripts/run_tests.sh tests/plugins/test_kanban_duplicate_title_gate.py
    mv local-plugins/kanban-duplicate-gate{.off,}

The branch's only diff against its base is the added plugin directory and this
file, so the red run exercises source that is byte-identical to the base commit.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator, Optional

import pytest

from hermes_cli import kanban_db as kb
from model_tools import handle_function_call
from tools import kanban_tools

PLUGIN_SRC = Path(__file__).resolve().parents[2] / "local-plugins" / "kanban-duplicate-gate"
PLUGIN_NAME = "kanban-duplicate-gate"

BODY = (
    "Prometheus rule TargetDown is FIRING.\n- host: llmbox / 192.168.1.97:8009\n"
    "- firing since 2026-11-13T11:16:07Z\n- value: 0e+00\nCheck the service, then the firewall.\n"
)


def _gate_module() -> ModuleType:
    """The plugin's pure rule, imported by path (no discovery, no hooks)."""
    spec = importlib.util.spec_from_file_location("dup_gate_under_test", PLUGIN_SRC / "gate.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def gate_plugin(_hermetic_environment: None) -> Iterator[bool]:
    """Install the plugin into this test's home, then load it through real discovery.

    ``_hermetic_environment`` (tests/conftest.py) has already pointed ``HERMES_HOME``
    at a temp home, and the plugin manager is resolved *per home* — so the plugin is
    installed into that same home's ``plugins/`` (enabled in its ``config.yaml``) or
    the dispatch path would read a registry that never loaded it. Real manifest, real
    loader, real hook registration; nothing is monkeypatched.

    RED run: rename the plugin directory aside and run this same file —
    ``mv local-plugins/kanban-duplicate-gate{,.off}``. Nothing subscribes to
    ``pre_tool_call`` then, so every duplicate is created and the refusal
    assertions fail. A missing source directory cannot produce a false green:
    with no gate installed the tests fail by construction.
    """
    if not PLUGIN_SRC.is_dir():
        yield False
        return

    home = Path(os.environ["HERMES_HOME"])
    shutil.copytree(PLUGIN_SRC, home / "plugins" / PLUGIN_NAME)
    (home / "config.yaml").write_text(
        f"plugins:\n  enabled:\n    - {PLUGIN_NAME}\n  disabled: []\n", encoding="utf-8"
    )
    from hermes_cli.plugins import discover_plugins, has_hook

    discover_plugins(force=True)
    assert has_hook("pre_tool_call"), "the plugin did not load — the rest would be a false green"
    yield True


@pytest.fixture
def board(gate_plugin: bool) -> Path:
    """A fresh board DB for one test (the original fixture's isolation, kept)."""
    db_path = kb.kanban_db_path()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    kb.init_db()
    return db_path


@contextmanager
def board_conn(db_path: Path) -> Iterator[sqlite3.Connection]:
    from hermes_cli import kanban_db_connect as kbc

    conn = kbc.connect(db_path=db_path)
    try:
        yield conn
    finally:
        conn.close()


def create(title: str, body: Optional[str] = BODY, **extra) -> str:
    """One ``kanban_create`` through the real agent dispatch; returns the tool result."""
    args: dict = {"title": title, "assignee": "ops"}
    if body is not None:
        args["body"] = body
    args.update(extra)
    return handle_function_call("kanban_create", args)


def task_id(result: str) -> str:
    payload = json.loads(result)
    assert payload.get("ok") is True, result
    return payload["task_id"]


def refusal(result: str) -> str:
    payload = json.loads(result)
    assert "task_id" not in payload, f"a card was created: {result}"
    return payload["error"]


def count_tasks(db_path: Path) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()


# --- the nine tests, one per rule -------------------------------------------------

def test_identical_title_and_body_rejected(board: Path):
    first = task_id(create("TargetDown: llmbox"))
    message = refusal(create("TargetDown: llmbox"))
    assert first in message
    assert count_tasks(board) == 1
    # the rule itself still raises, with the same attributes as the in-core version
    gate = _gate_module()
    with pytest.raises(gate.DuplicateCardError) as caught:
        gate.check([(first, BODY)], "TargetDown: llmbox", BODY)
    assert caught.value.existing_id == first


def test_title_match_ignores_case_and_whitespace(board: Path):
    first = task_id(create("TargetDown: llmbox"))
    assert first in refusal(create("  targetdown: LLMBOX "))


def test_only_ids_numbers_and_timestamps_differ_is_still_duplicate(board: Path):
    other = BODY.replace("8009", "8010").replace("11:16:07", "12:01:44").replace("0e+00", "1e+01")
    first = task_id(create("TargetDown: llmbox"))
    assert first in refusal(create("TargetDown: llmbox", other))


def test_materially_different_body_is_allowed(board: Path):
    different = (
        "Rotate the Loki ingest key on .37 and update the Alloy config on every host. "
        "Steps: mint a new key, push it, restart Alloy, confirm logs flow, revoke the old key. "
        "Owner is Evan; needs one hands-on session."
    )
    a = task_id(create("TargetDown: llmbox"))
    b = task_id(create("TargetDown: llmbox", different))
    assert a != b
    assert count_tasks(board) == 2
    # control: an identical body on that title IS refused, so the difference above
    # is what let the second card through — not a missing gate
    assert a in refusal(create("TargetDown: llmbox"))


def test_different_title_is_never_blocked(board: Path):
    a = task_id(create("TargetDown: llmbox"))
    b = task_id(create("TargetDown: pulsarr"))
    assert a != b
    assert count_tasks(board) == 2
    # control: the gate is live on the first title
    assert a in refusal(create("TargetDown: llmbox"))


def test_done_and_archived_cards_do_not_block_a_refile(board: Path):
    a = task_id(create("Disk churn"))
    with board_conn(board) as conn:
        kb.archive_task(conn, a)
    b = task_id(create("Disk churn"))
    # control: while b is live, the same refile is refused
    assert b in refusal(create("Disk churn"))
    with board_conn(board) as conn:
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (b,))
        conn.commit()
    task_id(create("Disk churn"))
    assert count_tasks(board) == 3


def test_blocked_and_triage_cards_count_as_open(board: Path):
    first = task_id(create("Decide X", initial_status="blocked"))
    assert first in refusal(create("Decide X"))
    second = task_id(create("Ping", triage=True))
    assert second in refusal(create("Ping", triage=True))


def test_idempotency_key_hit_still_returns_existing_card(board: Path):
    a = task_id(create("T", idempotency_key="k1"))
    assert task_id(create("T", idempotency_key="k1")) == a
    assert count_tasks(board) == 1
    # ...and the key does not shield a new card on the same title from the gate
    assert a in refusal(create("T"))


def test_empty_bodies_on_same_title_are_duplicates(board: Path):
    first = task_id(create("Ping", body=None))
    assert first in refusal(create("Ping", body=None))
    assert count_tasks(board) == 1

