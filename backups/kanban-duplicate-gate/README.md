# Backup: the uncommitted duplicate-card gate found in production's checkout

Taken 2026-11-16 from /home/claude/.hermes/hermes-agent (production's source checkout),
which was on branch `deploy/kanban-pause-pr143945` at commit b48dcf1dbc9623cd405140b521e9d5d853f8f66d.

Nothing here was committed there. All three files were uncommitted working-tree state.

| file | what it is | source mtime (UTC) |
|---|---|---|
| duplicate-card-gate.patch | the +52 line working-tree change to hermes_cli/kanban_db.py | 2026-11-13 16:40:39 |
| test_kanban_duplicate_title_gate.py | its 9 tests (untracked in that checkout) | 2026-11-13 16:38:06 |
| test_kanban_review_separation.py | unrelated untracked tests for the review-separation work; their implementation lives on the checkout's local branch `kanban/review-separation` (07d7b2ed63) and in upstream PR 144283 | 2026-11-09 05:06:33 |

The gate was live, not dormant: every process started from that checkout after 16:40 on the 13th
imports hermes_cli/kanban_db.py from it. Both running services (hermes-serve on :9119, hermes-gateway)
started on the 15th, after the edit.

Apply the patch to a clean checkout of b48dcf1dbc to reconstruct the working tree:
    git apply duplicate-card-gate.patch
