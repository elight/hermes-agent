"""The per-profile dispatch cap: one resolution, shared by every dispatch entry point.

Four places start a dispatcher pass, and until now each decided the per-profile cap for
itself — three of them decided it was nothing:

* ``gateway/kanban_watchers_dispatcher.py`` — the gateway tick, resolves
  ``kanban.max_in_progress_per_profile`` into ``_DispatcherSettings`` at boot.
* ``hermes_cli/kanban_ops.py::_cmd_dispatch`` — ``hermes kanban dispatch``, reads the same
  key but falls back to *no cap* when the key is absent, is ``0``, or the config raises.
* ``hermes_cli/kanban_db_dispatch.py::run_daemon`` — the deprecated standalone loop, which
  passes no per-profile cap at all despite its own docstring ("the standalone daemon must not
  be the one uncapped entry point").
* ``plugins/kanban/dashboard/plugin_api.py::dispatch`` — the dashboard's "nudge" button,
  ``max=8`` by default, no per-profile cap.

That fallback is the bug: an operator running a forced pass to unstick a starved board
started five workers for one profile in one pass, each booting a full model context. It is
the hazard core already refuses for the *global* cap — ``resolve_max_in_progress`` applies a
memory-derived default precisely because "unbounded fan-out swap-thrashes small hosts" — so a
missing per-profile cap defaulting to *unbounded* was the inconsistent direction to fail.

Two ways to opt out stay deliberate and stay the operator's, never a side effect of a bad
read: ``kanban.max_in_progress_per_profile: 0`` in config (the documented "0 opts out"), and
``--ignore-cap`` on the forced pass, which prints what it gave up.

It owns what the two ``kanban.*`` cap keys MEAN, and nothing else: no database, no board, no
spawn. Every dispatch entry point calls it before it opens a connection, which is the point —
they had each invented their own answer to "what is this profile's cap" and three of them
answered "none".

The module sits next to the dispatcher rather than inside it because ``kanban_db_dispatch`` is
over its file-size cap and can only shrink, and because a rule this small should be readable
in one screen.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Workers one profile may start per pass when nothing configured says otherwise. One is
#: the operator rule on the box this was filed from, and the safest floor: a forced pass
#: that starts a single worker still unsticks a board, it just does it at the gateway's rate.
FORCED_PASS_PROFILE_CAP_DEFAULT = 1

# Why a caller got the cap it got, so each entry point can say so out loud. "defaulted" and
# "bypassed" are the two an operator should never learn about from a log line they didn't
# ask for — the first is reported by the pass itself, the second only ever happens because
# the operator typed --ignore-cap.
SOURCE_CONFIGURED = "configured"
SOURCE_OPTED_OUT = "opted-out"
SOURCE_BYPASSED = "bypassed"
SOURCE_DEFAULTED = "defaulted"


def resolve_profile_cap(configured: Any, *, ignore_cap: bool = False,
                        default: int = FORCED_PASS_PROFILE_CAP_DEFAULT,
                        ) -> tuple[Optional[int], str]:
    """Resolve the per-profile cap for one dispatch pass → ``(cap, source)``.

    ``cap`` of ``None`` means "no cap this pass" — reachable only by an explicit opt-out
    (``0``) or an explicit bypass (``ignore_cap``), never by a value that failed to read.

    ``configured`` is whatever ``kanban.max_in_progress_per_profile`` yielded: an int, a
    numeric string, ``0``, ``None`` (key absent), or anything unparseable.
    """
    if ignore_cap:
        return None, SOURCE_BYPASSED
    if isinstance(configured, bool):
        # ``True``/``False`` are ints in Python; a YAML ``max_in_progress_per_profile: true``
        # is a malformed value, not a cap of one. Treat it as unreadable and fail closed.
        return default, SOURCE_DEFAULTED
    if isinstance(configured, int):
        value = configured
    else:
        try:
            value = int(str(configured).strip())
        except (TypeError, ValueError):
            return default, SOURCE_DEFAULTED
    if value > 0:
        return value, SOURCE_CONFIGURED
    if value == 0:
        return None, SOURCE_OPTED_OUT
    # Negative is a typo, not a policy. Uncapping on a typo is the failure this exists to
    # stop, so a nonsense value defaults rather than opting out.
    return default, SOURCE_DEFAULTED


def configured_profile_cap() -> Any:
    """``kanban.max_in_progress_per_profile`` exactly as written, or None when unset.

    Deliberately raw. ``0`` is a decision and an unparseable value is an accident; the
    caller has to be able to tell them apart, which is what :func:`resolve_profile_cap`
    is for — so this returns what the file said, not what it meant.
    """
    return _kanban_setting("max_in_progress_per_profile")


def configured_max_in_progress() -> Optional[int]:
    """Read ``kanban.max_in_progress`` from config, or None when unset/invalid.

    Shared so every dispatch entry point agrees on "explicitly configured": a
    positive integer wins, anything else falls through to the derived default.

    LIVES HERE, NOT IN THE DISPATCHER. ``kanban_db_dispatch`` is over its file-size cap and
    can only shrink, and a cap read does not belong scattered one per entry point anyway —
    this module owns what the ``kanban.*`` cap keys mean. It is re-exported from
    ``kanban_db_dispatch``, where ``run_daemon`` and the gateway still resolve the name, and
    where tests keep patching it.
    """
    raw = _kanban_setting("max_in_progress")
    if raw is None:
        return None
    try:
        ival = int(raw)
    except (TypeError, ValueError):
        return None
    return ival if ival >= 1 else None


def _kanban_setting(key: str) -> Any:
    """One key from the ``kanban:`` block, or None. Read-only; never writes config.

    A broken config file must not brick a dispatch pass, and it must not read as "no cap"
    either — None here means "nothing configured", which :func:`resolve_profile_cap`
    resolves fail-closed. The traceback is logged so a malformed config stays visible.
    """
    try:
        from hermes_cli.config import load_config_readonly
        kanban = (load_config_readonly() or {}).get("kanban") or {}
        return kanban.get(key)
    except Exception:  # health: allow BLE001 -- config-read boundary; any failure means "unset", logged below
        logger.warning("kanban: could not read kanban.%s from config; treating it as unset",
                       key, exc_info=True)
        return None


def resolve_entry_point_cap(*, ignore_cap: bool = False) -> tuple[Optional[int], str]:
    """Read the config and resolve it in one step — what a dispatch path calls."""
    return resolve_profile_cap(configured_profile_cap(), ignore_cap=ignore_cap)
