"""C1 (t_c1f387b2): the ``ws closed`` teardown line names who ended the connection
and what the socket looked like when detection happened — the two fields that turn
production ``gui.log`` into a reconnect-latency baseline:

* ``life_s``   accept → close detection (the churn denominator).
* ``silent_s`` last inbound frame → detection: how long the socket sat orphaned and
  silent before the server declared it dead (the server-visible half of detection lag).
* ``initiated`` who closed: ``peer`` (normal client close codes), ``keepalive``
  (uvicorn/websockets ping-timeout codes — the C2a hypothesis signature), ``server``
  (our send/ready failure), ``shutdown``.

Log-only; no timer or teardown semantics change.
"""

import asyncio
import logging

from tui_gateway import server
from tui_gateway import ws as ws_mod


def _close_line(caplog, monkeypatch, exc_factory):
    """Drive handle_ws through accept → read failure → teardown; return the new ``ws closed`` line."""
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    server._sessions.clear()
    server._live_transports.clear()
    try:
        class FakeWS:
            async def accept(self, subprotocol=None):
                pass

            async def send_text(self, line):
                pass

            async def receive_text(self):
                raise exc_factory()

            async def close(self):
                pass

        before = len(caplog.records)
        with caplog.at_level(logging.INFO, logger="tui_gateway.ws"):
            asyncio.run(ws_mod.handle_ws(FakeWS()))
    finally:
        server._sessions.clear()
        server._live_transports.clear()

    lines = [r.getMessage() for r in caplog.records[before:] if r.getMessage().startswith("ws closed ")]
    assert len(lines) == 1, caplog.text
    return lines[0]


def _field(line, name):
    token = [part for part in line.split() if part.startswith(f"{name}=")]
    assert len(token) == 1, f"{name} missing from: {line}"
    return token[0][len(name) + 1:]


def test_peer_close_reports_life_silent_and_initiated_peer(caplog, monkeypatch):
    # 1005: no close frame status — the production code of the churned desktop sockets.
    line = _close_line(caplog, monkeypatch, lambda: ws_mod._WebSocketDisconnect(1005, ""))
    assert _field(line, "initiated") == "peer"
    assert float(_field(line, "life_s")) >= 0.0
    assert float(_field(line, "silent_s")) >= 0.0


def test_keepalive_timeout_code_is_labeled_keepalive_not_peer(caplog, monkeypatch):
    # The C2a falsification signature: a uvicorn/websockets ping-timeout close must be
    # distinguishable from a client close in the log.
    line = _close_line(caplog, monkeypatch,
                       lambda: ws_mod._WebSocketDisconnect(1011, "keepalive ping timeout"))
    assert _field(line, "initiated") == "keepalive"
    assert "keepalive ping timeout" in line  # raw reason retained verbatim


def test_server_send_failure_is_not_labeled_connected(caplog, monkeypatch):
    """A refused ready-frame write used to tear down with reason/initiated indistinguishable
    from a healthy connection: it must be attributed to the server."""
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    server._sessions.clear()
    server._live_transports.clear()
    try:
        class DeadSendWS:
            async def accept(self, subprotocol=None):
                pass

            async def send_text(self, line):
                raise RuntimeError("transport is gone")

            async def receive_text(self):  # pragma: no cover - never reached
                raise AssertionError("read loop must not start after a failed ready write")

            async def close(self):
                pass

        with caplog.at_level(logging.INFO, logger="tui_gateway.ws"):
            asyncio.run(ws_mod.handle_ws(DeadSendWS()))
    finally:
        server._sessions.clear()
        server._live_transports.clear()

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("ws closed "))
    assert _field(line, "reason") == "ready_send_failed"
    assert _field(line, "initiated") == "server"


def test_abnormal_close_code_is_not_claimed_as_keepalive(caplog, monkeypatch):
    # A protocol error close (1002) names no cause the C2a hypothesis predicts; the
    # line must not launder it into either causal bucket. 1005/1006 (peer vanished
    # without a close frame) do count as the peer leaving.
    line = _close_line(caplog, monkeypatch, lambda: ws_mod._WebSocketDisconnect(1002, "protocol"))
    assert _field(line, "initiated") == "unknown"

    vanished = _close_line(caplog, monkeypatch, lambda: ws_mod._WebSocketDisconnect(1006, ""))
    assert _field(vanished, "initiated") == "peer"
