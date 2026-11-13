"""Keepalive strike policy for non-loopback WebSocket listeners (C2a, t_e15288d4).

The measured production churn: laptop-app sockets die on a 40 s grid (p50 40.0 s; 99 % of
short-lived closes land within 0.25 s of a 20 s multiple), and the Step-1 harness reproduces it
through the production listener — a client whose process the OS suspends (App Nap, sleep,
SIGSTOP-class freeze) cannot answer pongs, and uvicorn's sansio keepalive fails it at exactly
``ws_ping_interval + ws_ping_timeout`` (the 20/20 non-loopback default: 40.0 s, code 1005).

One missed pong cycle is inconclusive for a backgrounded renderer: the kernel keeps ACKing while
the process is frozen, and a waking client answers the buffered ping within milliseconds. So
require two consecutive misses before failing the connection — but only when the whole detection
budget still fits under the proxy idle cap the non-loopback bind sits behind (docs cite the
Cloudflare Tunnel ~100 s). Half-open detection must stay inside that cap; where one longer
cycle already owns the budget (the SSH-tunnel path's 60/600), the policy leaves the transport's
own single-strike arithmetic alone.
"""
from __future__ import annotations

from typing import Any, Optional

#: Non-loopback binds sit behind proxies that drop idle connections at roughly this idle time;
#: a genuinely half-open connection must be detected before the proxy does.
WS_KEEPALIVE_DETECT_CAP_S = 100.0

#: Consecutive missed pong cycles that count as evidence the peer is gone.
DEFAULT_WS_PING_STRIKES = 2


def ws_ping_strikes(ping_interval: Optional[float], ping_timeout: Optional[float]) -> int:
    """Missed pongs to tolerate for a listener with this cadence.

    Two cycles only buy rescue time if detection still lands under ``WS_KEEPALIVE_DETECT_CAP_S``
    (defaults 20/20 → kill at 60 s ✓). A cadence that cannot fit two cycles under the cap is one
    the operator chose for a slow link; keep its single long cycle (60/600 → 660 s) rather than
    doubling it past every watchdog that depends on the detection.
    """
    if ping_interval is None or ping_timeout is None:  # pings off (loopback bind)
        return 1
    if ping_interval + DEFAULT_WS_PING_STRIKES * ping_timeout <= WS_KEEPALIVE_DETECT_CAP_S:
        return DEFAULT_WS_PING_STRIKES
    return 1


def _make_strike_protocol(base: type, strikes: int) -> type:
    class KeepaliveStrikeProtocol(base):  # type: ignore[valid-type]
        """``WebSocketsSansIOProtocol`` failing a connection only after N consecutive missed pongs.

        A first timeout re-pings immediately (the second strike's pong deadline is re-armed by
        ``send_keepalive_ping``); an in-flight pong clears the counter through ``handle_pong``,
        so a client that wakes inside the grace keeps its socket. The final strike defers to the
        transport's own failure path — the close report (1005 to the app, per
        ``tests/tui_gateway/test_ws_close_initiated.py``) is unchanged, only its timing moves.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._missed_pongs = 0

        def handle_pong(self, event) -> None:
            # Match against the pending payload BEFORE super() clears it: a matching pong means
            # the peer is alive however many strikes we have already counted.
            answered = (self.pending_ping_payload is not None
                        and bytes(event.data) == self.pending_ping_payload)
            super().handle_pong(event)
            if answered:
                self._missed_pongs = 0

        def keepalive_timeout(self) -> None:
            if (self._missed_pongs + 1 < strikes
                    and not self.close_sent and not self.transport.is_closing()):
                self._missed_pongs += 1
                self.pong_timer = None
                self.pending_ping_payload = None
                self.logger.debug("WebSocket keepalive ping timeout (strike %d/%d)",
                                  self._missed_pongs, strikes)
                self.send_keepalive_ping()  # arms the next pong deadline; handle_pong chains the rest
                return
            super().keepalive_timeout()

    return KeepaliveStrikeProtocol


_STRIKE_PROTOCOLS: dict[int, type[Any]] = {}


def strike_keepalive_protocol(ws_ping: tuple[Optional[float], Optional[float]]) -> Any:
    """The ``uvicorn.Config(ws=...)`` value for a listener with cadence *ws_ping* (interval, timeout);
    ``"auto"`` keeps the transport untouched.

    "auto" means "no change to the transport": pings off, a single-strike policy (identical to
    upstream), or a uvicorn whose auto transport is not the sansio implementation we can extend
    (no ``websockets`` package installed → uvicorn falls back to wsproto, whose keepalive is
    different code and keeps the old behavior). Returns ``Any`` because the value IS the
    ``WebSocketsSansIOProtocol`` subclass at runtime; uvicorn's own annotation wants
    ``type[Protocol]``, which a dynamic ``class X(base)`` is not statically.
    """
    strikes = ws_ping_strikes(ws_ping[0], ws_ping[1])
    if strikes <= 1:
        return "auto"
    cached = _STRIKE_PROTOCOLS.get(strikes)
    if cached is not None:
        return cached
    try:
        from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
    except ImportError:  # pragma: no cover -- the wsproto fallback keeps the cadence as-is
        return "auto"
    protocol = _make_strike_protocol(WebSocketsSansIOProtocol, strikes)
    _STRIKE_PROTOCOLS[strikes] = protocol
    return protocol
