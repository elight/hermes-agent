"""Regression for #147036: ``GET /api/config/schema`` builds the schema on the event loop.

``config_env.get_schema`` merged its discovery-driven provider options inline, so the
per-request schema walk plus the plugin-name scan ran on the uvicorn event loop that
also serves ``/api/ws`` and every other dashboard route — a slow scan froze the whole
gateway for its duration. The handler now offloads the build to a worker thread via
``config_scoped_to_thread``, the same helper its sibling ``get_config`` uses.
"""

import asyncio
import threading

import pytest

from hermes_cli.web_routers import config_env


def test_get_schema_builds_off_the_loop_thread(monkeypatch):
    """The schema build runs on a worker thread, never the event-loop thread."""
    idents = {}
    built = {"voice.stt.provider": {"type": "str", "options": ["stub"]}}

    def _build():
        idents["build"] = threading.get_ident()
        # A worker thread runs no event loop; prove we are off it.
        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()
        return built

    monkeypatch.setattr(config_env, "_schema_with_dynamic_provider_options", _build)

    async def _drive():
        idents["loop"] = threading.get_ident()
        return await config_env.get_schema(None)

    result = asyncio.run(_drive())

    assert "build" in idents, "the schema build never ran"
    assert idents["build"] != idents["loop"], "the schema build ran on the event-loop thread"
    assert result["fields"] is built
    assert result["category_order"] == config_env._CATEGORY_ORDER
