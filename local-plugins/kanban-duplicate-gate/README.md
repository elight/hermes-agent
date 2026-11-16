# kanban-duplicate-gate

Refuses a `kanban_create` when an open card already has the same title and a
description that is not materially different. Extracted from an uncommitted
working-tree change that ran inside `hermes_cli/kanban_db.py` (documented in the
kanban card `t_92f2a27c`); it is local board policy, not upstream behaviour.

## What it decides

A create is refused when **both** hold:

1. an *open* card exists with the same title — compared case-insensitively, with
   surrounding whitespace trimmed. Open is every status except `done` and
   `archived`, so `blocked` and `triage` cards both block a refile;
2. that card's body is **not materially different**: both bodies are normalised
   (lower-cased, card ids `t_<hex>`, timestamps and every number removed,
   whitespace collapsed) and compared with `difflib`; a similarity of
   **0.90 or more** is a duplicate. Two empty bodies on the same title are
   duplicates of each other.

An existing card carrying the new create's `idempotency_key` is left alone, so
the keyed repeat still returns that card instead of being refused.

A refusal creates nothing. The message names the open card and offers the three
ways out: comment on it, change the title, or write a materially different
description.

## Where it sits: the seam

| | |
|---|---|
| Hook | `pre_tool_call` (`hermes_cli/plugins.py` `VALID_HOOKS`) |
| Fired by | `model_tools._pre_dispatch_guards` → `hermes_cli.plugins._get_pre_tool_call_directive_details` (`hermes_cli/plugins.py:1116`) |
| Directive | `{"action": "block", "message": <refusal text>}` — the message becomes the tool result |
| Ordering | before `tools.registry.dispatch`, so the handler never runs and no row is written |

From that seam the plugin sees `tool_name` and the coerced tool arguments
(`title`, `body`, `assignee`, `board`, `idempotency_key`, …) plus session/turn
ids. It does **not** get the kanban connection, the board handle, or any view of
creates that never pass through tool dispatch.

**Coverage:** the agent-facing `kanban_create` tool call — which is what an
orchestrator or worker profile uses. Not covered: `hermes kanban create` (CLI),
the dashboard/plugin API, and in-process `kanban_db.create_task` callers
(specifier, decompose, swarm, transfer). Full coverage needs a directive-capable
pre-create hook inside `create_task`; that is upstream's seam to add, not
something this plugin can reach.

## The one private import

`board.board_db_path()` calls `hermes_cli.kanban_db.kanban_db_path()` — the same
resolver the `kanban_create` handler uses, so the plugin and the writer can never
disagree about which board is in play (env pins, the board argument, the
`kanban/current` symlink and the archived-board guards are all non-trivial).

That module is *not* on the public plugin API (`hermes_cli/data/plugin_api.json`),
so `hermes plugins validate` warns and `scripts/boundary_lint.py` counts it as a
core crossing. The alternatives were worse: re-deriving the board path in the
plugin would silently diverge from the writer, and the refusal is worthless if it
checks the wrong board. Isolated here so it is one line to change when a seam
lands upstream.

Everything else is public: the hook, the directive, and plain `sqlite3`.

## Install / enable

The directory *is* the plugin. Put it where the profiles that need it can load
it, and enable it per profile (`plugins.enabled` in that profile's `config.yaml`):

```bash
cp -r local-plugins/kanban-duplicate-gate ~/.hermes/profiles/<profile>/plugins/
hermes -p <profile> plugins enable kanban-duplicate-gate
```

## Tests

`tests/plugins/test_kanban_duplicate_title_gate.py` — the nine tests of the
original working-tree gate, ported to drive the real dispatch path
(`model_tools.handle_function_call("kanban_create", …)`), one per rule:

```bash
# green: plugin loaded through real discovery from a temp HERMES_HOME
pytest tests/plugins/test_kanban_duplicate_title_gate.py

# red: same file, plugin not installed — nothing refuses the second create
HERMES_TEST_DUP_GATE=0 pytest tests/plugins/test_kanban_duplicate_title_gate.py
```
