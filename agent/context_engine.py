"""Abstract base class for pluggable context engines.

A context engine decides when/how conversation context is compacted near the token
limit, tracks usage, and may expose tools. ContextCompressor is the default;
``context.engine`` selects a plugin (``plugins/context_engine/<name>/``); one is active.
Lifecycle: on_session_start() -> per API response update_from_response() -> per turn
should_compress() / compress() -> on_session_end() at real session boundaries only
(CLI exit, /reset, gateway expiry), never per-turn.
"""

import copy
import json
import logging
from contextlib import suppress
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from agent.compression_marker import elide_middle
from agent.redact import redact_sensitive_text

logger = logging.getLogger(__name__)


MEMORY_CONTEXT_MAX_CHARS = 6_000
_MEMORY_CONTEXT_HEAD_CHARS = 4_000
_MEMORY_CONTEXT_TAIL_CHARS = 1_500


def sanitize_memory_context(memory_context: str) -> str:
    """Prepare provider context for a context-engine/LLM egress boundary."""
    sanitized = redact_sensitive_text(memory_context.strip(), force=True, redact_url_credentials=True)
    if len(sanitized) <= MEMORY_CONTEXT_MAX_CHARS:
        return sanitized
    return elide_middle(sanitized, _MEMORY_CONTEXT_HEAD_CHARS, _MEMORY_CONTEXT_TAIL_CHARS)


class CompactionProgressText(str):
    """A status line its producer marks as compaction in progress (the lifecycle status kind stays).

    Gateways re-tag only these as ``kind="compacting"``: a warning that merely mentions compression
    is not progress, and an engine may reword the progress lines. Like ``DiagnosticText``, the mark
    lives on the object, so classify before ``str()``/``strip()``/formatting drop it.
    """


def automatic_compaction_status_message(
    engine: Any, *, phase: str, default_message: str, **context: Any
) -> CompactionProgressText | None:
    """Host-visible status for an automatic compaction event; ``None`` = emit nothing.

    Engines suppress via ``emit_automatic_compaction_status = False`` or
    customize via ``get_automatic_compaction_status_message(...)``.
    """
    if not getattr(engine, "emit_automatic_compaction_status", True):
        return None
    formatter = getattr(engine, "get_automatic_compaction_status_message", None)
    message = formatter(phase=phase, default_message=default_message, **context) if callable(formatter) else default_message
    if message is None:
        return None
    text = str(message).strip()
    return CompactionProgressText(text) if text else None


class ContextEngine(ABC):
    """Base class all context engines must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g. 'compressor', 'lcm')."""

    # Token state: engines MUST maintain these; run_agent.py reads them directly.
    last_prompt_tokens: int = 0
    last_completion_tokens: int = 0
    last_total_tokens: int = 0
    threshold_tokens: int = 0
    context_length: int = 0
    compression_count: int = 0
    # Compaction parameters (read by run_agent.py for preflight). protect_first_n counts
    # non-system head messages kept verbatim IN ADDITION to the always-protected system
    # prompt (3 keeps the historical head shape).
    # These control the preflight compression check. Subclasses may override via __init__ or property;
    # defaults are sensible for most engines. See #13754.
    threshold_percent: float = 0.75
    protect_first_n: int = 3
    protect_last_n: int = 6
    # False keeps successful automatic compaction passes silent (routine background
    # maintenance); warnings, errors and manual /compress still surface.
    emit_automatic_compaction_status: bool = True

    @abstractmethod
    def update_from_response(self, usage: dict[str, Any]) -> None:
        """Update tracked token usage after every LLM call.

        ``prompt_tokens``/``completion_tokens``/``total_tokens`` are always present; the
        canonical buckets (``input_tokens``, ``output_tokens``, ``cache_read_tokens``,
        ``cache_write_tokens``, ``reasoning_tokens``) are optional on older hosts.
        """

    @abstractmethod
    def should_compress(self, prompt_tokens: int | None = None) -> bool:
        """Return True if compaction should fire this turn."""

    def should_compress_info(self, prompt_tokens: int | None = None) -> tuple[bool, str | None]:
        """Return ``(should_compress, reason)``.

        Engines with block reasons (summary-LLM cooldown, anti-thrashing guard) override
        this so callers can warn instead of silently skipping; the default keeps plugin
        engines from raising AttributeError.
        """
        return self.should_compress(prompt_tokens), None

    @abstractmethod
    def compress(
        self, messages: list[dict[str, Any]], current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None, force: bool = False, memory_context: str = "",
    ) -> list[dict[str, Any]]:
        """Compact ``messages`` into a valid OpenAI-format list that fits the budget.

        ``focus_topic`` comes from manual ``/compress <focus>`` (prioritise that topic);
        ``force`` asks to bypass an engine-owned cooldown; ``memory_context`` is provider
        text for the handoff prompt. Older engines may omit optional parameters — the
        host filters them by signature.
        """

    def prune_tool_results_only(
        self, messages: list[dict[str, Any]], current_tokens: int | None = None, *, cold_cache: bool = False,
        turn_start: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Deterministically trim old tool-result payloads without an LLM call.

        Runs on a low, cost-oriented trigger independent of ``should_compress`` so
        large-window engines reclaim re-sent tool output long before full compaction.
        ``cold_cache`` says the provider prompt cache already expired, so a rewrite costs
        no cache break; ``turn_start`` indexes the user row of the turn in progress, whose
        results must stay whole. The host passes each only to engines whose signature accepts it.
        Returns ``(messages, n_pruned)``; the default no-op keeps older engines safe.
        """
        return messages, 0

    def select_context(
        self, request_messages: list[dict[str, Any]], *, conversation_messages: list[dict[str, Any]] | None = None,
        incoming_message: dict[str, Any] | None = None, budget_tokens: int = 0,
    ) -> list[dict[str, Any]]:
        """Optionally *select* (replace) the context for THIS request, pre-generation.

        Runs on every provider request (also retries), independent of
        ``should_compress()``: ``compress()`` shrinks over-long context, this swaps in a
        different one (retrieval, topic routing, branch switching). Return ``None`` to
        leave the request unchanged. The returned list is request-only — it MUST NOT be
        treated as persisted transcript state (session DB history is untouched); unlike
        ``pre_llm_call`` it may replace the list. The host runs it before prompt
        cache-control and every request sanitizer, so a malformed replacement never
        reaches the provider and the default no-op keeps the request byte-identical;
        an engine that replaces the list changes its own cache prefix (breakpoints are
        re-derived on the selected list). ``request_messages`` is the assembled request
        (system prompt + history + ephemeral prefill); ``conversation_messages`` is the
        persisted history for reference only (do not mutate); ``budget_tokens`` is the
        model's context length or 0 if unknown.
        """
        return None

    def on_turn_complete(self, messages: list[dict[str, Any]], usage: dict[str, Any] | None = None, **kwargs: Any) -> None:
        """Observe a finished turn (complement of ``select_context()``) to index/update
        routing state for the next request.

        Best-effort, not guaranteed: fires from the normal finalization seam only; some
        abnormal early returns (content-policy block, provider terminal failure) skip it.
        ``messages`` is a read-only shallow copy (return value ignored; never rely on
        transcript mutation). ``usage`` has the ``update_from_response`` shape and is
        ``None`` when no provider response was reached (interrupt). ``kwargs`` may include
        ``turn_id``, ``task_id``, ``api_call_count``, ``interrupted``, ``failed``, ``turn_exit_reason``.
        """
        return

    def should_compress_preflight(self, messages: list[dict[str, Any]]) -> bool:
        """Cheap rough check before the API call (no real token count yet); default skips."""
        return False

    def should_defer_preflight_to_real_usage(self, rough_tokens: int) -> bool:
        """True when preflight should trust recent real usage over the noisy rough
        estimate (avoids re-compacting after a compressed request already fit)."""
        return False

    def get_automatic_compaction_status_message(
        self, *, phase: str, default_message: str, **context: Any,
    ) -> str | None:
        """User-visible status for automatic compaction, or ``None`` to suppress it.

        ``phase`` is the host call site (``"preflight"`` / ``"compress"``); ``context``
        carries best-effort ``approx_tokens`` / ``threshold_tokens``. Warnings, errors
        and manual ``/compress`` are not governed by this hook.
        """
        return default_message if self.emit_automatic_compaction_status else None

    def has_content_to_compress(self, messages: list[dict[str, Any]]) -> bool:
        """Preflight guard for gateway ``/compress``: False reports "nothing to
        compress yet" without an LLM call (e.g. transcript entirely protected)."""
        return True

    def on_session_start(self, session_id: str, **kwargs) -> None:
        """Session begins: load persisted state. kwargs may include hermes_home, platform, model."""

    def on_session_end(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """Real session boundary (CLI exit, /reset, gateway expiry) — never per-turn."""

    def on_session_reset(self) -> None:
        """/new or /reset: reset per-session state (default: counters and token tracking)."""
        # Reset cross-call calibration state captured under the PREVIOUS model. These fields encode "the
        # provider proved this prompt fit" / "preflight can be deferred" decisions that are only valid for
        # the model that produced them. Carrying them across a switch to a smaller-context model would let
        # should_defer_preflight_to_real_usage() suppress a preflight compression the new model actually
        # needs — the exact oversized-send-after-switch failure in #23767. The new model's first response
        # repopulates them via update_from_response(). Setting last_prompt_tokens to 0 (NOT -1) is
        # deliberate: 0 is the documented "no real usage yet -> use the rough estimate" state, so the post-
        # response should_compress path falls back to estimate_request_tokens_rough rather than skipping
        # compression. -1 is a different sentinel (#36718, "compression just ran, await real usage") and
        # must not be set here.
        self.last_prompt_tokens = 0
        self.last_completion_tokens = 0
        self.last_total_tokens = 0
        self.compression_count = 0

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """Tool schemas this engine exposes to the agent (default: none)."""
        return []

    def handle_tool_call(self, name: str, args: dict[str, Any], **kwargs) -> str:
        """Handle a call to one of this engine's tools; must return a JSON string.
        kwargs may include ``messages`` (live in-memory list)."""
        return json.dumps({"error": f"Unknown context engine tool: {name}"})

    def get_status(self) -> dict[str, Any]:
        """Status dict with the standard fields run_agent.py expects."""
        # Clamp the -1 "compression just ran, awaiting real usage" sentinel to 0 so no
        # reader sees a negative usage_percent on the transitional turn.
        last_prompt = max(self.last_prompt_tokens, 0)
        return {
            "last_prompt_tokens": last_prompt,
            "threshold_tokens": self.threshold_tokens,
            "context_length": self.context_length,
            "usage_percent": min(100, last_prompt / self.context_length * 100) if self.context_length else 0,
            "compression_count": self.compression_count,
        }

    def clone_for_agent(self) -> "ContextEngine":
        """Per-agent instance of a plugin-registered engine (the plugin system holds ONE shared
        instance; every AIAgent gets its own so a child's update_model() cannot mutate the parent's).
        Override when the engine holds uncopyable state (locks, DB connections): return a fresh
        engine sharing the durable backend and copying only mutable budget state."""
        return copy.deepcopy(self)

    def update_model(
        self, model: str, context_length: int, base_url: str = "", api_key: str = "",
        provider: str = "", api_mode: str = "",
    ) -> None:
        """Model switch / fallback: recompute threshold_tokens (override for more).

        Per-model threshold override (longest substring match), else the raw config
        percent — snapshotted ONCE so repeated switches fall back to the configured
        value, not the previous model's override.
        """
        self.context_length = context_length
        from agent.context_compressor import resolve_model_threshold
        if not hasattr(self, "_config_threshold_percent"):
            self._config_threshold_percent = self.threshold_percent
        self._base_threshold_percent = resolve_model_threshold(
            model, getattr(self, "model_thresholds", {}), self._config_threshold_percent, provider)
        self.threshold_percent = self._base_threshold_percent
        self.threshold_tokens = int(context_length * self.threshold_percent)


class ContextEngineNotFound(RuntimeError):
    """Raised at session start when ``context.engine`` names an engine that no plugin
    provides, even after a forced plugin rescan.

    A configured engine that is absent is a config/runtime contradiction, not a warning:
    silently compressing every conversation on the profile with the built-in compressor
    is a fleet-scale degradation (2026-11-28: profiles pinned to ``lcm-x`` ran the
    built-in compressor while the plugin was installed+enabled, because the multiplex
    gateway's per-profile plugin registry was frozen at process start)."""


def lookup_context_engine_by_name(engine_name):
    """Resolve one configured engine by name. Returns ``(engine, copy_failed)``.

    Leg 1: ``$HERMES_HOME/plugins/context_engine/<name>/`` — a context-engine plugin
    dropped into the user's plugins dir, selected by name, no ``plugins.enabled`` entry
    needed (#61839). Leg 2: the engine a plugin registered via
    ``ctx.register_context_engine()`` — one shared instance, so each agent gets its own
    via ``clone_for_agent()`` (#42449). ``copy_failed`` marks an engine that EXISTS but
    could not be copied — the documented built-in-compressor fallback, never the
    not-found path.
    """
    try:
        from plugins.context_engine import load_context_engine
        selected = load_context_engine(engine_name)
    except Exception:  # health: allow BLE001 -- a broken third-party engine loader is a miss, not a crash
        logger.debug("Context engine load from plugins/context_engine/ failed", exc_info=True)
        selected = None
    if selected is not None:
        return selected, False
    try:
        from hermes_cli.plugins import get_plugin_context_engine
        candidate = get_plugin_context_engine()
    except Exception:  # health: allow BLE001 -- plugin system unavailable is a miss, not a crash
        logger.debug("Plugin context engine lookup failed", exc_info=True)
        candidate = None
    if candidate is None or candidate.name != engine_name:
        return None, False
    # The plugin system holds ONE shared instance; each agent gets its own so a child's
    # update_model() can't mutate the parent's (#42449). clone_for_agent() defaults to
    # deepcopy; engines with uncopyable state (locks, DB conns) override it. A failure
    # falls back to the built-in compressor with an ACCURATE message, not "not found".
    try:
        return candidate.clone_for_agent(), False
    except Exception as copy_err:  # health: allow BLE001 -- documented #42449 fallback contract
        logger.warning(
            "Context engine '%s' could not be safely copied for this "
            "agent (%s) — falling back to built-in compressor. Plugin "
            "engines that hold uncopyable state (locks, DB connections) "
            "should override clone_for_agent() (or __deepcopy__) to copy "
            "only mutable budget state.",
            engine_name, copy_err,
        )
        return None, True


def refresh_plugins_and_retry_engine(engine_name):
    """One forced plugin rescan + lookup retry, for long-lived processes.

    A multiplex gateway resolves plugin discovery once per profile home
    (``PluginManager._discovered``); an engine plugin enabled — or ``context.engine``
    selected — AFTER that first scan stays invisible to every later session on the
    profile, even though a fresh process binds it. Session init is the refresh point:
    the forced scan re-reads the profile's ``plugins.enabled`` and re-runs plugin
    registration, so an enable takes effect without a gateway restart. A healthy
    profile (engine already bound) never reaches here.
    """
    try:
        from hermes_cli.plugins import discover_plugins
        discover_plugins(force=True)
    except Exception:  # health: allow BLE001 -- a broken rescan must still reach the retry and the error path
        logger.warning("Forced plugin rescan while resolving context engine '%s' failed",
                       engine_name, exc_info=True)
    return lookup_context_engine_by_name(engine_name)


def select_context_engine(agent_cfg):
    """Config-driven context engine: ``context.engine`` → plugins/context_engine/<name>/ →
    general plugin system → None (built-in ContextCompressor).

    A configured engine that no plugin provides is retried once through a forced plugin
    rescan (:func:`refresh_plugins_and_retry_engine`) and then raises
    :class:`ContextEngineNotFound` — session start fails loud, naming the engine and the
    profile, instead of silently degrading every conversation on it to the built-in
    compressor."""
    from agent.safe_worker_policy import safe_worker_enabled

    if safe_worker_enabled():
        return None
    engine_name = "compressor"
    with suppress(Exception):
        ctx_cfg = agent_cfg.get("context", {}) if isinstance(agent_cfg, dict) else {}
        engine_name = ctx_cfg.get("engine", "compressor") or "compressor"
    if engine_name == "compressor":
        return None  # built-in; don't auto-activate plugins
    selected, copy_failed = lookup_context_engine_by_name(engine_name)
    if selected is None and not copy_failed:
        selected, copy_failed = refresh_plugins_and_retry_engine(engine_name)
    if selected is None and not copy_failed:
        # A copy failure already warned with the accurate reason (#42449 contract:
        # built-in fallback, never a raise). Only a genuinely missing engine fails loud.
        from hermes_constants import get_hermes_home
        raise ContextEngineNotFound(
            f"Configured context engine '{engine_name}' was not found in this profile "
            f"({get_hermes_home()}) even after a forced plugin rescan. Install or enable "
            "a plugin that registers an engine with this exact name (`hermes plugins "
            "list`), or set context.engine back to 'compressor'. Failing session start "
            "rather than silently compressing with the built-in compressor.")
    return selected
