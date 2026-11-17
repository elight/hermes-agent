"""Which card opened a PR — ``active_pr`` must not park a card over a sibling's PR it cites.

Regression for #147231 item 2 ("any comment on the task with a PR URL anchors the hold"),
in the guard step of :func:`hermes_cli.kanban_db_dispatch.check_respawn_guard` that treats
*any* GitHub PR URL in *any* comment on a card as "this card's prior worker already opened
a PR". Comments are also written about other cards, so a citation parked an unrelated ready
card for the whole 24 h window.

Seeded from a real board (not invented): a deploy/QA gate card — assignee ``code-monkey``,
all four parents ``done`` — was held for hours with zero workers spawned. The gateway
reported it every five minutes: ``kanban dispatcher stuck: ready queue non-empty for 256
consecutive ticks but 0 workers spawned. Last tick held back: active_pr=1, paused=10``.
The comment that held it (author ``code-monkey``, the card's OWN assignee, so comment
authorship cannot tell the two cards apart) read::

    Finding for this card's battery, from #C6 (`t_44f96ba3`, PR
    https://github.com/elight/hermes-agent/pull/14).

pull/14 belongs to ``#C6``: that card's own workers recorded it on their closing runs. The
measured run metadata (real bytes) keys the URL under ``pr`` / ``pr_head``, not under any
fixed name — the key is free-form worker prose, so the URL *value* is what identifies a
publication, and a reader that looks only for one key name sees nothing::

    {"branch": "c6/t_44f96ba3-resume-runtimes-on-reconnect", "commit": "d8b8e9e5c6",
     "pr": "https://github.com/elight/hermes-agent/pull/14",
     "pr_base": "reconnect-optimization", "pr_state": "OPEN, MERGEABLE, head == pushed sha"}

The gate card has no record of any PR at all. Nothing about it says "an implementer is
waiting on a review"; only the citation does.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

PUBLISHED_PR = "https://github.com/elight/hermes-agent/pull/14"
OTHER_PR = "https://github.com/elight/hermes-agent/pull/13"

# Verbatim shape of the comment that held the real card (comment ids differ only in the
# prose the QA worker wrapped around it).
SIBLING_CITATION = (
    "Finding for this card's battery, from #C6 (`t_44f96ba3`, PR %s).\n\n"
    "`evals/liveness/ws_orphan_reconnect.py` is RED at the branch tip before #C6's diff."
) % PUBLISHED_PR


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture(autouse=True)
def _spawnable_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)


def _publish_pr(conn, task_id: str, url: str, *, profile: str = "dev", age_seconds: int = 7200) -> None:
    """Record ``url`` as a PR ``task_id``'s own worker published.

    Byte-for-byte the shape the real board writes: ``kanban_db`` lands the worker's
    metadata dict on the closing run, and the worker names the PR under its own
    free-form key (measured: ``pr``). Asserted after the write so a fixture that
    silently stops matching the real schema fails here, not in a false green.

    The closing run is ``review_requested`` (the measured outcome on the real
    publisher's runs) and is backdated past the 1 h ``recent_success`` window, so what
    the assertions below measure is the ``active_pr`` step and nothing else.
    """
    now = int(time.time())
    metadata = {
        "branch": "c6/t_44f96ba3-resume-runtimes-on-reconnect",
        "commit": "d8b8e9e5c6",
        "pr": url,
        "pr_state": "OPEN, MERGEABLE, head == pushed sha",
        "worker_session_id": "local-fixture",
    }
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " summary, metadata) VALUES (?, ?, 'completed', 'review_requested', ?, ?, ?, ?)",
            (task_id, profile, now - age_seconds - 60, now - age_seconds,
             f"Opened {url}", json.dumps(metadata)),
        )
    stored = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()["metadata"]
    assert url in json.loads(stored).values(), stored


def _record_url_on_closing_run(conn, task_id: str, key: str, url: str, *, ended_at: int,
                               profile: str = "reviewer") -> None:
    """Land a closing run on ``task_id`` whose metadata files ``url`` under ``key`` at ``ended_at``.

    NOT the same thing as publishing the PR. A QA, review, follow-up or ship card files the
    PR it worked on as a matter of course (measured: ``elight/catsdb/pull/14`` is recorded on
    two cards; ``elight/llmbox/pull/38`` on the card that opened it AND on an ADR-renumber
    card that worked on it afterwards). So a record answers "who handled this PR", and the
    only ordering the guard can read out of it is ``ended_at`` — metadata lands when the run
    closes, which is the instant the card started vouching for the URL.
    """
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " summary, metadata) VALUES (?, ?, 'completed', 'completed', ?, ?, ?, ?)",
            (task_id, profile, ended_at - 60, ended_at, f"worked on {url}", json.dumps({key: url})),
        )


def _comment(conn, task_id: str, body: str, *, author: str = "dev") -> int:
    """Add a comment and return its stored epoch, so sibling records can be timed against it."""
    kb.add_comment(conn, task_id, author=author, body=body)
    row = conn.execute(
        "SELECT created_at FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    stamp = kb._to_epoch(row["created_at"])
    assert stamp is not None, f"fixture comment has no parseable stamp: {row['created_at']!r}"
    return stamp


def _guard(conn, task_id: str) -> str | None:
    return kbd.check_respawn_guard(conn, task_id)


def test_cited_sibling_pr_does_not_park_the_citing_card(kanban_home: Path) -> None:
    """The measured incident: a citation of a sibling's published PR must not hold.

    Before the fix the gate card returns ``active_pr`` and is never spawned, while the
    sibling's PR — not this card's — is what the comment is about.
    """
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="C6 sibling implementation", assignee="dev")
        _publish_pr(conn, sibling, PUBLISHED_PR)

        gate = kb.create_task(conn, title="deploy + QA gate", assignee="dev")
        kb.add_comment(conn, gate, author="dev", body=SIBLING_CITATION)

        assert _guard(conn, gate) is None
        spawned = [s[0] for s in kbd.dispatch_once(conn, dry_run=True).spawned]
        assert gate in spawned


def test_own_published_pr_still_parks_the_card(kanban_home: Path) -> None:
    """The protection stays: the card that published the PR is not re-spawned against it."""
    with kbc.connect() as conn:
        own = kb.create_task(conn, title="implementer own pr", assignee="dev")
        _publish_pr(conn, own, PUBLISHED_PR)
        kb.add_comment(conn, own, author="dev", body=f"Opened {PUBLISHED_PR} for review.")

        assert _guard(conn, own) == "active_pr"
        res = kbd.dispatch_once(conn, dry_run=True)
        assert own not in [s[0] for s in res.spawned]
        assert dict(res.respawn_guarded)[own] == "active_pr"


def test_pr_url_no_card_is_recorded_as_publishing_still_parks(kanban_home: Path) -> None:
    """Fail closed, as today: a worker that reported its PR only in prose stays protected."""
    with kbc.connect() as conn:
        fresh = kb.create_task(conn, title="reported in prose only", assignee="dev")
        kb.add_comment(conn, fresh, author="dev", body=f"Opened {PUBLISHED_PR} for review.")

        assert _guard(conn, fresh) == "active_pr"


def test_both_publisher_and_citer_are_recorded_parks_the_citer(kanban_home: Path) -> None:
    """Ambiguity never costs a duplicate PR: if the citing card also names the PR in its own
    run record, authorship is not decidable and the guard holds."""
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling", assignee="dev")
        _publish_pr(conn, sibling, PUBLISHED_PR)
        both = kb.create_task(conn, title="cites it AND names it in its own run", assignee="dev")
        _publish_pr(conn, both, PUBLISHED_PR)
        kb.add_comment(conn, both, author="dev", body=SIBLING_CITATION)

        assert _guard(conn, both) == "active_pr"


def test_comment_mixing_a_foreign_pr_with_an_unattributed_pr_parks_the_card(kanban_home: Path) -> None:
    """One unknown URL in the comment is enough: the fix can only narrow the guard."""
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling", assignee="dev")
        _publish_pr(conn, sibling, PUBLISHED_PR)
        mixed = kb.create_task(conn, title="two PRs, one unattributed", assignee="dev")
        kb.add_comment(
            conn, mixed, author="dev",
            body=f"Battery note, from the sibling (PR {PUBLISHED_PR}); my own run also "
                 f"touched {OTHER_PR} but recorded nothing.",
        )

        assert _guard(conn, mixed) == "active_pr"


def test_pr_number_is_matched_whole_not_as_a_prefix(kanban_home: Path) -> None:
    """``pull/1`` is not ``pull/14``: a sibling's low-numbered PR must not lend its
    attribution to a citation of a different PR that nobody published."""
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling owns pull/1", assignee="dev")
        _publish_pr(conn, sibling, "https://github.com/elight/hermes-agent/pull/1")
        gate = kb.create_task(conn, title="gate cites pull/14", assignee="dev")
        kb.add_comment(conn, gate, author="dev", body=SIBLING_CITATION)

        assert _guard(conn, gate) == "active_pr"


def test_pr_number_is_matched_whole_in_either_direction(kanban_home: Path) -> None:
    """The other prefix direction: a recorded ``pull/140`` must not vouch for a cited
    ``pull/14``. The SQL candidate filter matches on a substring by design; the whole-key
    comparison is what stops a bigger PR number laundering a citation of a smaller one."""
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling owns pull/140", assignee="dev")
        _publish_pr(conn, sibling, "https://github.com/elight/hermes-agent/pull/140")
        gate = kb.create_task(conn, title="gate cites pull/14", assignee="dev")
        kb.add_comment(conn, gate, author="dev", body=SIBLING_CITATION)

        assert _guard(conn, gate) == "active_pr"


def test_attribution_survives_a_differently_spelled_url(kanban_home: Path) -> None:
    """Detection stays as broad as the regex it replaces (``http``, any case) and the
    attribution still recognises the same PR: a spelling difference must neither skip the
    guard silently nor hide a sibling's publication."""
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling", assignee="dev")
        _publish_pr(conn, sibling, PUBLISHED_PR)
        gate = kb.create_task(conn, title="gate cites it sloppily", assignee="dev")
        kb.add_comment(
            conn, gate, author="dev",
            body="Battery finding, from the sibling: "
                 "HTTP://GitHub.COM/Elight/Hermes-Agent/pull/14 is red at its tip.",
        )

        assert _guard(conn, gate) is None


def test_an_unattributed_pr_url_parks_the_card_whatever_its_spelling(kanban_home: Path) -> None:
    """The counterpart to the case above, and the one that catches a narrowed matcher: an
    unattributed PR written as ``HTTP://`` in mixed case is still a PR this card may have
    opened, so it must still hold the lane. A detector that only read canonical ``https``
    would lift here — silently widening a fail-closed rule."""
    with kbc.connect() as conn:
        fresh = kb.create_task(conn, title="prose only, sloppy spelling", assignee="dev")
        kb.add_comment(
            conn, fresh, author="dev",
            body="Opened HTTP://GitHub.COM/Elight/Hermes-Agent/pull/14 for review.",
        )

        assert _guard(conn, fresh) == "active_pr"


def test_contract_binding_on_another_card_does_not_lift_the_hold(kanban_home: Path) -> None:
    """A sibling's ``completion_contract`` is a declaration, not a dated publication.

    Round-1 review: the acceptance store may bind an exact PR URL into
    ``tasks.completion_contract`` for ANY card that declares it — including a review or QA
    card whose acceptance *is* someone else's PR — and the binding carries no timestamp the
    guard can order against a comment. So it can identify this card's own PR, but it can
    never prove a sibling published it first. Measured: 0 cards on all 13 local boards bind
    a PR URL in a contract, so nothing real depended on the old lift.
    """
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling bound its contract", assignee="dev")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (PUBLISHED_PR, sibling))
        gate = kb.create_task(conn, title="gate cites it", assignee="dev")
        kb.add_comment(conn, gate, author="dev", body=SIBLING_CITATION)

        assert _guard(conn, gate) == "active_pr"
        assert _guard(conn, sibling) is None  # the owner has no PR comment; nothing to hold


def test_own_contract_anchors_even_though_a_sibling_recorded_first(kanban_home: Path) -> None:
    """This card's own record wins over an earlier sibling's: it still holds the lane.

    Otherwise a card that declared the PR as its contract could be de-protected by some other
    card that filed the same URL earlier, which is the fail-open this round is about.
    """
    with kbc.connect() as conn:
        earlier = kb.create_task(conn, title="recorded the URL long ago", assignee="reviewer")
        _record_url_on_closing_run(conn, earlier, "pr_reviewed", PUBLISHED_PR,
                                   ended_at=int(time.time()) - 86_400)
        own = kb.create_task(conn, title="declared the PR as its contract", assignee="dev")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET completion_contract = ? WHERE id = ?", (PUBLISHED_PR, own))
        kb.add_comment(conn, own, author="dev", body=f"Opened {PUBLISHED_PR} for review.")

        assert _guard(conn, own) == "active_pr"


# --------------------------------------------------------------------------- round 2: the opener keeps its protection
#
# Review round 1 found the fail-open in the round-1 rule: it treated ANY card whose run
# metadata contains the URL as that PR's publisher, and filing a handled PR on your own run
# is routine. The opener's protection — the #111910 case, a worker that reported its PR only
# in comment prose — was the first casualty, because a downstream card that recorded the PR
# afterwards lifted the hold on the very card that opened it.


def test_a_later_mention_on_another_card_keeps_the_openers_hold(kanban_home: Path) -> None:
    """The reviewer's repro, verbatim. The card that spoke first keeps its protection.

    1. Card P comments "Opened .../pull/14" and has NO closing-run metadata (worker crashed
       or reported only in prose) — the #111910 case the rule promises to protect.
    2. Unrelated card Q later closes a run whose metadata is ``{"pr_under_test": .../pull/14}``.
    3. P must still be held. If Q's record frees P, a second worker is spawned against the PR
       P already opened and a duplicate implementation follows.
    """
    with kbc.connect() as conn:
        opener = kb.create_task(conn, title="opened the PR, said so only in prose", assignee="dev")
        said_at = _comment(conn, opener, f"Opened {PUBLISHED_PR} for review.")

        handler = kb.create_task(conn, title="later QA card that filed the PR it tested", assignee="qa")
        _record_url_on_closing_run(conn, handler, "pr_under_test", PUBLISHED_PR, ended_at=said_at + 30)

        assert _guard(conn, opener) == "active_pr"
        res = kbd.dispatch_once(conn, dry_run=True)
        assert opener not in [s[0] for s in res.spawned]
        assert dict(res.respawn_guarded)[opener] == "active_pr"


def test_sibling_record_ending_before_the_comment_lifts_the_citation(kanban_home: Path) -> None:
    """The incident's own timing must keep working: the sibling published 3 s BEFORE the cite.

    Measured on the real board — publisher run 146 on ``t_44f96ba3`` ended at 1794635684,
    comment 44 on the stuck gate card ``t_d66742ba`` was written at 1794635687. A record that
    predates the comment cannot have been written about the comment's card, so the citation is
    coordination prose and the lane frees.
    """
    with kbc.connect() as conn:
        gate = kb.create_task(conn, title="deploy + QA gate", assignee="dev")
        said_at = _comment(conn, gate, SIBLING_CITATION)
        sibling = kb.create_task(conn, title="C6 sibling implementation", assignee="dev")
        _record_url_on_closing_run(conn, sibling, "pr", PUBLISHED_PR, ended_at=said_at - 3)

        assert _guard(conn, gate) is None
        assert gate in [s[0] for s in kbd.dispatch_once(conn, dry_run=True).spawned]


def test_sibling_record_ending_in_the_same_second_keeps_the_hold(kanban_home: Path) -> None:
    """Same-second tie: ordering is undecidable, so the guard holds — as it does for a
    same-second handoff event in the step that follows."""
    with kbc.connect() as conn:
        opener = kb.create_task(conn, title="opener", assignee="dev")
        said_at = _comment(conn, opener, f"Opened {PUBLISHED_PR} for review.")
        handler = kb.create_task(conn, title="handler", assignee="qa")
        _record_url_on_closing_run(conn, handler, "pr_under_test", PUBLISHED_PR, ended_at=said_at)

        assert _guard(conn, opener) == "active_pr"


def test_sibling_record_from_a_run_still_open_keeps_the_hold(kanban_home: Path) -> None:
    """An open run has no ``ended_at``: nothing vouched for that URL yet, so no lift.

    Real boards do have such rows (1 on ``hermes-agent``, 2 on ``island-of-misfit-cards``).
    """
    with kbc.connect() as conn:
        opener = kb.create_task(conn, title="opener", assignee="dev")
        _comment(conn, opener, f"Opened {PUBLISHED_PR} for review.")
        running = kb.create_task(conn, title="still running", assignee="qa")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
                " summary, metadata) VALUES (?, ?, 'running', NULL, ?, NULL, ?, ?)",
                (running, "qa", int(time.time()) - 60, "working", json.dumps({"pr": PUBLISHED_PR})),
            )

        assert _guard(conn, opener) == "active_pr"


def test_unreadable_comment_timestamp_keeps_the_hold(kanban_home: Path) -> None:
    """A comment whose own stamp cannot be parsed cannot be out-ordered, so nothing lifts.

    ``_rows_since`` substitutes ``now`` for an unreadable comment stamp on purpose, so that
    the comment still *anchors*; that substituted value must not double as a real timestamp
    for the attribution lookup, or garbage in ``created_at`` would become a way to lift the
    hold on any card that has a PR in flight.
    """
    with kbc.connect() as conn:
        opener = kb.create_task(conn, title="opener", assignee="dev")
        _comment(conn, opener, f"Opened {PUBLISHED_PR} for review.")
        with kb.write_txn(conn):
            conn.execute("UPDATE task_comments SET created_at = 'later' WHERE task_id = ?", (opener,))
        handler = kb.create_task(conn, title="handler", assignee="qa")
        _record_url_on_closing_run(conn, handler, "pr_under_test", PUBLISHED_PR,
                                   ended_at=int(time.time()) - 600)

        assert _guard(conn, opener) == "active_pr"


def test_review_lane_is_unaffected_by_the_attribution_rule(kanban_home: Path) -> None:
    """The lane exemption is untouched: a PR comment is a review handoff's INPUT, whether
    the PR is this card's or a sibling's."""
    with kbc.connect() as conn:
        sibling = kb.create_task(conn, title="sibling", assignee="dev")
        _publish_pr(conn, sibling, PUBLISHED_PR)
        review = kb.create_task(conn, title="review me", assignee="reviewer")
        claimed = kb.claim_task(conn, review)
        assert claimed is not None
        kb.add_comment(conn, review, author="dev", body=SIBLING_CITATION)
        assert kb.request_review(
            conn, review, summary="battery reviewed", expected_run_id=claimed.current_run_id,
        )
        assert kbd.check_respawn_guard(conn, review, lane="review") is None
