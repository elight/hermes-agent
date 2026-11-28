"""A configured ``context.engine`` resolves per-session, not per-process (card t_35e7655d).

Live fleet failure, 2026-11-28 (receipts: t_041ab0c2 comment 940 / EVIDENCE.md): the
``lcm-x`` plugin was installed and enabled and ``context.engine: lcm-x`` was set on
cron/libby/tidings while the multiplex gateway ran. ``config get`` agreed; a FRESH
process bound ``LCMEngine lcm-x``; gateway-served sessions kept logging
``Context engine 'lcm-x' not found — falling back to built-in compressor`` — the
per-home ``PluginManager._discovered`` snapshot was frozen at the profile's first scan.
A config that names an engine the runtime cannot find is a contradiction, not a
warning: silently compressing every conversation with the wrong engine is invisible.

Invariants:
  * a plugin enabled AFTER the profile's first discovery is bound by the very next
    session, with no process restart and no test-only cache poke (the forced rescan
    rides ``discover_plugins(force=True)``, the same lever the dashboard enable path
    and the ``reload-plugins`` verb pull);
  * a healthy profile pays nothing: an engine already in the registry never triggers
    a rescan;
  * a configured engine nothing provides — after the rescan — raises
    ``ContextEngineNotFound`` naming the engine and the profile home, instead of
    warn-and-degrade;
  * the documented #42449 copy-failure contract is UNCHANGED: an engine that exists
    but cannot be cloned falls back to the built-in compressor with a warning, never
    a raise, and never through a pointless rescan.
"""

import logging
import textwrap

import pytest

from agent.agent_init import ContextEngineNotFound, _select_context_engine

_ENGINE_IMPL = textwrap.dedent('''
    from agent.context_engine import ContextEngine

    class _ReproEngine(ContextEngine):
        @property
        def name(self):
            return "ctx_repro"

        def update_from_response(self, usage):
            pass

        def should_compress(self, prompt_tokens=None):
            return False

        def compress(self, messages, current_tokens=None):
            return list(messages)

    def register(ctx):
        ctx.register_context_engine(_ReproEngine())
''')

# __init__.py deliberately avoids the "ContextEngine" / "register_context_engine"
# substrings: the leg-1 user-dir engine loader (_is_context_engine_dir) is a stateless
# text heuristic that would find the engine fresh on every call and defeat the whole
# point. With a clean __init__, the engine reaches the registry ONLY through the
# enabled-gated general plugin system (leg 2) — exactly the stale path the live
# gateway hit with the installed ``hermes_plugins.hermes_lcm_x`` package.
_ENGINE_INIT = "from .impl import register  # noqa: F401\n"


def _enable_config(enabled):
    return "plugins:\n  enabled: [" + ", ".join(enabled) + "]\n"


def _home(tmp_path, enabled):
    """A Hermes home with the engine plugin installed and ``plugins.enabled`` as given."""
    plugin = tmp_path / "plugins" / "ctx_repro"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(
        "name: ctx_repro\nversion: '0.1'\ndescription: repro engine\n")
    (plugin / "impl.py").write_text(_ENGINE_IMPL)
    (plugin / "__init__.py").write_text(_ENGINE_INIT)
    (tmp_path / "config.yaml").write_text(_enable_config(enabled))
    return tmp_path


@pytest.fixture()
def rescan_spy(monkeypatch):
    import hermes_cli.plugins as plugins_mod
    calls = []
    real = plugins_mod.discover_plugins

    def _spy(force=False):
        calls.append(force)
        return real(force=force)

    monkeypatch.setattr(plugins_mod, "discover_plugins", _spy)
    return calls


def test_engine_enabled_after_first_discovery_binds_without_restart(tmp_path, monkeypatch,
                                                                     rescan_spy):
    """Repro of the live gateway freeze, in-process.

    The first session for the profile runs while ``ctx_repro`` is NOT enabled — that
    discovery pass is the snapshot the running gateway holds forever. Enabling the
    plugin afterwards must bind the engine on the very next session selection, no
    restart and no test-only registry reset between the two legs."""
    from hermes_cli.plugins import discover_plugins, get_plugin_context_engine

    home = _home(tmp_path, enabled=[])
    monkeypatch.setenv("HERMES_HOME", str(home))

    # Leg 1 — the frozen snapshot: first discovery happens before the enable. This is
    # the live incident shape (engine configured, plugin installed but NOT enabled):
    # the session must fail loud, not silently compress with the built-in compressor.
    discover_plugins()
    assert get_plugin_context_engine() is None
    with pytest.raises(ContextEngineNotFound):
        _select_context_engine({"context": {"engine": "ctx_repro"}})

    # Flip plugins.enabled mid-process — exactly what `hermes plugins enable` / the
    # dashboard writes.
    (home / "config.yaml").write_text(_enable_config(["ctx_repro"]))

    # Leg 2 — same process, same managers: session init must refresh and bind.
    engine = _select_context_engine({"context": {"engine": "ctx_repro"}})
    assert engine is not None, "engine enabled after first discovery was not bound"
    assert engine.name == "ctx_repro"
    assert True in rescan_spy, "session init never forced the plugin rescan"


def test_bound_engine_does_not_trigger_a_rescan(tmp_path, monkeypatch, rescan_spy):
    """Hot-path guard: a profile whose engine is already registered pays no forced scan."""
    home = _home(tmp_path, enabled=["ctx_repro"])
    monkeypatch.setenv("HERMES_HOME", str(home))

    engine = _select_context_engine({"context": {"engine": "ctx_repro"}})
    assert engine is not None and engine.name == "ctx_repro"
    assert rescan_spy.count(True) == 0

    again = _select_context_engine({"context": {"engine": "ctx_repro"}})
    assert again is not None and again.name == "ctx_repro"
    assert rescan_spy.count(True) == 0


def test_configured_missing_engine_raises_naming_engine_and_profile(tmp_path, monkeypatch):
    home = _home(tmp_path, enabled=[])
    monkeypatch.setenv("HERMES_HOME", str(home))

    with pytest.raises(ContextEngineNotFound) as exc:
        _select_context_engine({"context": {"engine": "no_such_engine"}})

    msg = str(exc.value)
    assert "no_such_engine" in msg
    assert home.name in msg, f"profile home not named in error: {msg}"


def test_copy_failure_still_falls_back_and_never_rescans(tmp_path, monkeypatch, caplog,
                                                         rescan_spy):
    """#42449 contract preserved: an engine that EXISTS but cannot be cloned keeps the
    warn-and-use-built-in behaviour — it must not raise, and must not pay a rescan."""
    class _Uncopyable:
        name = "ctx_broken"

        def clone_for_agent(self):
            raise RuntimeError("deepcopy hit a live SQLite handle")

    home = _home(tmp_path, enabled=["ctx_broken"])
    monkeypatch.setenv("HERMES_HOME", str(home))
    import plugins.context_engine as user_dir
    import hermes_cli.plugins as plugins_mod
    monkeypatch.setattr(user_dir, "load_context_engine", lambda name: None)
    monkeypatch.setattr(plugins_mod, "get_plugin_context_engine", lambda: _Uncopyable())

    with caplog.at_level(logging.WARNING):
        assert _select_context_engine({"context": {"engine": "ctx_broken"}}) is None

    assert "could not be safely copied" in caplog.text
    assert "not found" not in caplog.text
    assert rescan_spy.count(True) == 0
