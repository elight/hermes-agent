"""Building a listener's ``uvicorn.Config`` must leave Hermes's own file logging alone.

uvicorn's default ``log_config`` runs ``logging.config.dictConfig``, which calls
``logging.shutdown()`` on every live handler. A multiplexed gateway installs its profile log
routers before the API listener starts, and a closed router stays closed, so agent.log,
errors.log and gateway.log went silent for every home until restart. Real handlers and files;
nothing binds.
"""

import logging
import os
import sys

import pytest

import hermes_cli.web_server as ws_mod
import hermes_logging
from hermes_cli.web_server_lifecycle import build_listener_server
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")


@pytest.fixture(autouse=True)
def _isolated_logging():
    """Undo the queued file handlers, root level and uvicorn logger state a test sets up."""
    root = logging.getLogger()
    saved_root_level = root.level
    saved = {name: (lg.level, lg.propagate, list(lg.handlers))
             for name in _UVICORN_LOGGERS for lg in (logging.getLogger(name),)}
    hermes_logging._reset_queued_handlers()
    yield
    hermes_logging._reset_queued_handlers()
    hermes_logging._logging_initialized = False
    root.setLevel(saved_root_level)
    for name, (level, propagate, handlers) in saved.items():
        logger = logging.getLogger(name)
        for handler in logger.handlers:
            if handler not in handlers:
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)
        logger.propagate = propagate


def _gateway_listener(_monkeypatch):
    return build_listener_server(lambda scope, receive, send: None, "127.0.0.1", 0, auth_required=False)


def _dashboard_listener(monkeypatch):
    monkeypatch.setattr(ws_mod.app.state, "auth_required", False, raising=False)
    return ws_mod._build_uvicorn_server("127.0.0.1", 0)


@pytest.mark.parametrize("build", [_gateway_listener, _dashboard_listener], ids=["gateway", "dashboard"])
def test_listener_config_keeps_every_profile_log_file_writing(tmp_path, monkeypatch, build):
    root = tmp_path / "root"
    beta = root / "profiles" / "beta"
    beta.mkdir(parents=True)
    (beta / "config.yaml").write_text("{}", encoding="utf-8")
    hermes_logging.setup_logging(hermes_home=root, mode="gateway", force=True)
    assert hermes_logging.enable_profile_log_routing([root, beta])
    logger = logging.getLogger("gateway.listener_logging")

    def log_in_every_home(marker):
        for home in (root, beta):
            token = set_hermes_home_override(str(home))
            try:
                logger.warning(marker)
            finally:
                reset_hermes_home_override(token)
        hermes_logging.flush_log_queue()

    log_in_every_home("before-listener")
    build(monkeypatch)
    log_in_every_home("after-listener")
    for home in (root, beta):
        for name in ("agent.log", "errors.log", "gateway.log"):
            text = (home / "logs" / name).read_text(encoding="utf-8-sig")
            assert "before-listener" in text
            assert "after-listener" in text, f"building the listener silenced {home / 'logs' / name}"


def test_listener_config_keeps_uvicorn_on_the_console_and_out_of_the_hermes_logs(tmp_path, monkeypatch, capsys):
    """Without uvicorn's ``dictConfig`` its loggers still print where they did: ``uvicorn.error``
    to stderr, access lines nowhere at ``log_level="warning"``, neither into agent.log."""
    root = tmp_path / "root"
    hermes_logging.setup_logging(hermes_home=root, mode="gateway", force=True)
    _gateway_listener(monkeypatch)
    _dashboard_listener(monkeypatch)  # a second listener in the process adds no second console handler

    logging.getLogger("uvicorn.error").warning("uvicorn-console-warning")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "GET", "/access-line", "1.1", 200)
    logging.getLogger("gateway.listener_logging").warning("hermes-file-warning")
    hermes_logging.flush_log_queue()

    out, err = capsys.readouterr()
    assert logging.getLogger("uvicorn").level == logging.INFO  # LOGGING_CONFIG's, not the root's
    assert err.count("uvicorn-console-warning") == 1
    assert "/access-line" not in out + err
    agent_log = (root / "logs" / "agent.log").read_text(encoding="utf-8-sig")
    assert "hermes-file-warning" in agent_log
    assert "uvicorn-console-warning" not in agent_log and "/access-line" not in agent_log


def test_uvicorn_console_handler_journals_one_record_per_log(tmp_path, monkeypatch):
    """The listener's console handlers are journal-aware.

    ``hermes serve`` runs under systemd with its stdout/stderr in the journal, where
    every newline becomes its OWN record at PRIORITY=6 — a uvicorn traceback would
    cost one `info` record per line and lie about its level. On a journal-marked
    stream the handler must emit one line, carrying the record's real priority.
    """
    root = tmp_path / "root"
    hermes_logging.setup_logging(hermes_home=root, mode="gateway", force=True)
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    info = os.fstat(write_fd)
    monkeypatch.setenv("JOURNAL_STREAM", f"{info.st_dev}:{info.st_ino}")
    stream = os.fdopen(write_fd, "w", encoding="utf-8")
    monkeypatch.setattr(sys, "stderr", stream)
    for name in _UVICORN_LOGGERS:  # so the listener installs its console handler
        logging.getLogger(name).handlers[:] = []
    try:
        _gateway_listener(monkeypatch)
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1,
            "listener failed\nTraceback (most recent call last):\n  RuntimeError: port busy", (), None)
        logging.getLogger("uvicorn").handle(record)
        written = os.read(read_fd, 8192).decode()
    finally:
        stream.close()
        os.close(read_fd)

    assert written.count("\n") == 1, written
    assert written.startswith("<3>"), written
    assert "listener failed" in written and "RuntimeError: port busy" in written
