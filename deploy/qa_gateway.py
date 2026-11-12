#!/usr/bin/env python3
"""qa_gateway.py — the QA machine every reconnect test card drives.

Runs against the TEST Hermes (:10119) with zero privileged access: HTTP via the
standard library, one WebSocket library, and the test profile's own dashboard
credentials. There is no SSH, no systemctl, no production access anywhere in
this file, and it never writes to the server.

Commands
--------
  ticket   Sign in, mint a ws ticket, report elapsed ms. Drives both mint
           endpoints the app uses: POST /api/webapp/socket-ticket (the Webapp
           page's one-use ticket) and POST /api/auth/ws-ticket (the session's).
           The first exists only on the gateway's Webapp listener surface; the
           dashboard here answers 404 there, and open/phases use the one that
           answers — named in every report.
  open     Sign in, mint a ticket, open /api/ws on it, report elapsed ms for
           the mint and for the upgrade.
  phases   Run the reconnect sequence N times and print the phase table with
           p50/p95 (names per C1's schema in
           apps/desktop/src/store/desktop-metrics.ts):
              detect_ms pre_ipc_ms dial_ms ready_ms replay_barrier_ms usable_ms
           A gateway-side driver can only observe part of that schema: it
           closes the socket on purpose (detect_ms = 0.0 by construction) and
           it has no client replay to wait behind. Those columns print n/a and
           are filled in by C1's client-side instrumentation; the measurable
           ones are real measurements of the test instance.

Credentials
-----------
Read from the test profile's .env (default
~/.hermes/profiles/hermes-test/.env), never from the command line, the repo or
a card:
    HERMES_DASHBOARD_BASIC_AUTH_USERNAME
    HERMES_DASHBOARD_BASIC_AUTH_PASSWORD
Environment variables of the same name win when set.

Examples
--------
    scripts/run-in-hermes-env python deploy/qa_gateway.py ticket
    scripts/run-in-hermes-env python deploy/qa_gateway.py open
    scripts/run-in-hermes-env python deploy/qa_gateway.py phases --cycles 20
    # over Tailscale, from any QA machine on the tailnet:
    ... deploy/qa_gateway.py --base-url https://llmbox.tail291e4.ts.net:10119 phases --cycles 20
"""

from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import os
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "http://127.0.0.1:10119"
DEFAULT_ENV_FILE = os.path.expanduser("~/.hermes/profiles/hermes-test/.env")
AUTH_PROVIDER = "basic"
PHASES = ("detect_ms", "pre_ipc_ms", "dial_ms", "ready_ms", "replay_barrier_ms", "usable_ms")
#: The two ticket endpoints, in the order the app uses them. Only a surface whose
#: SurfacePolicy sets ``gateway_ticket`` (the gateway's Webapp listener) serves the
#: first; the dashboard answers 404 there and its own ticket is the second.
TICKET_ENDPOINTS = (
    ("POST /api/webapp/socket-ticket",
     "/api/webapp/socket-ticket?" + urllib.parse.urlencode({"socket": "gateway"})),
    ("POST /api/auth/ws-ticket", "/api/auth/ws-ticket"),
)
#: How long the socket waits for anything else the server wants to say.
READY_BUDGET_S = 2.0
RPC_BUDGET_S = 5.0


# --------------------------------------------------------------------------
# credentials
# --------------------------------------------------------------------------
def read_env_file(path: str) -> dict[str, str]:
    """Minimal dotenv reader (KEY=VALUE, optional quotes, # comments)."""
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                key = key.strip()
                # strip an optional `export ` prefix
                if key.startswith("export "):
                    key = key[len("export "):].strip()
                value = raw.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                values[key] = value
    except FileNotFoundError:
        raise SystemExit(f"qa_gateway: env file not found: {path}")
    return values


def credentials(env_file: str) -> tuple[str, str]:
    file_values = read_env_file(env_file)
    username = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_USERNAME") or file_values.get(
        "HERMES_DASHBOARD_BASIC_AUTH_USERNAME", "")
    password = os.environ.get("HERMES_DASHBOARD_BASIC_AUTH_PASSWORD") or file_values.get(
        "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD", "")
    if not username or not password:
        raise SystemExit(
            "qa_gateway: no dashboard credentials. Set HERMES_DASHBOARD_BASIC_AUTH_USERNAME and "
            "HERMES_DASHBOARD_BASIC_AUTH_PASSWORD, or point --env-file at the test profile's .env "
            f"(default {DEFAULT_ENV_FILE}). Credentials are never passed on the command line."
        )
    return username, password


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Http:
    """Cookie-carrying JSON client for the gated test dashboard."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        scheme = urllib.parse.urlparse(self.base_url).scheme
        self._jar = http.cookiejar.CookieJar()
        handlers: list[urllib.request.BaseHandler] = [urllib.request.HTTPCookieProcessor(self._jar)]
        if scheme == "https":
            # The tailnet URL is a real cert; the loopback default is plain http.
            handlers.append(urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self._opener = urllib.request.build_opener(*handlers)

    def send(self, method: str, path: str, body: dict | None = None) -> tuple[int, str]:
        """Raw HTTP: (status, body text). A non-2xx status is data, not an exception —
        surface-specific endpoints answer 404 where another surface mints."""
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except urllib.error.URLError as exc:
            raise SystemExit(f"qa_gateway: {method} {path} -> {exc.reason}") from exc

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        status, text = self.send(method, path, body)
        if status != 200:
            raise SystemExit(f"qa_gateway: {method} {path} -> HTTP {status}: {text[:300]}")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}

    def login(self, username: str, password: str) -> None:
        self.request("POST", "/auth/password-login",
                     {"provider": AUTH_PROVIDER, "username": username, "password": password})

    def _mint(self, path: str) -> tuple[str, float, int]:
        """One ticket mint: (ticket, elapsed_ms, status); ticket is '' when refused."""
        t0 = time.perf_counter()
        status, text = self.send("POST", path)
        elapsed = (time.perf_counter() - t0) * 1000.0
        if status != 200:
            return "", elapsed, status
        try:
            return str(json.loads(text).get("ticket", "")), elapsed, status
        except json.JSONDecodeError:
            return "", elapsed, status

    def mint_ticket(self) -> tuple[str, float, str]:
        """The ticket for ONE ``/api/ws`` dial on this server: (ticket, ms, source).

        ``POST /api/webapp/socket-ticket`` is the Webapp page's one-use ticket and
        exists only on a surface whose SurfacePolicy sets ``gateway_ticket`` — the
        gateway's Webapp listener (``hermes webapp``), not this dashboard. The
        dashboard surface answers 404 there; its own ticket is
        ``POST /api/auth/ws-ticket`` (session cookie, one per socket). Whichever
        answers is returned, named, so every measurement says where it came from.
        """
        webapp_path = "/api/webapp/socket-ticket?" + urllib.parse.urlencode({"socket": "gateway"})
        ticket, elapsed, _status = self._mint(webapp_path)
        if ticket:
            return ticket, elapsed, "webapp"
        ticket, elapsed, _status = self._mint("/api/auth/ws-ticket")
        if ticket:
            return ticket, elapsed, "auth"
        raise SystemExit(
            "qa_gateway: neither ticket endpoint minted a ticket. Is the login cookie valid "
            "(credentials) and the listener up?")

    def ws_url(self, ticket: str) -> str:
        parsed = urllib.parse.urlparse(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        query = urllib.parse.urlencode({"ticket": ticket})
        return urllib.parse.urlunparse((scheme, parsed.netloc, "/api/ws", "", query, ""))


# --------------------------------------------------------------------------
# WebSocket
# --------------------------------------------------------------------------
def _load_websockets():
    try:
        import websockets  # noqa: F401  (import here so --help works without it)
    except ImportError as exc:  # pragma: no cover - environment guard
        raise SystemExit(
            "qa_gateway: the 'websockets' package is required (run inside the checkout's "
            "environment: scripts/run-in-hermes-env python deploy/qa_gateway.py ...)"
        ) from exc
    return websockets


def phase_values(rows: list[dict[str, float | None]], name: str) -> list[float]:
    """One phase's measured values across cycles (n/a rows are skipped)."""
    out: list[float] = []
    for row in rows:
        value = row[name]
        if value is not None:
            out.append(value)
    return out


def percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    if len(ordered) == 1:
        return ordered[0]
    index = q * (len(ordered) - 1)
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f}"


def print_table(rows: list[dict[str, float | None]], cycles: int, sources: set[str]) -> None:
    header = "cycle".ljust(6) + "".join(name.rjust(19) for name in PHASES)
    print(header)
    print("-" * len(header))
    for i, row in enumerate(rows, start=1):
        print(str(i).ljust(6) + "".join(fmt(row[name]).rjust(19) for name in PHASES))
    print("-" * len(header))
    for label, q in (("p50", 0.50), ("p95", 0.95)):
        cells = []
        for name in PHASES:
            values = phase_values(rows, name)
            cells.append(fmt(percentile(values, q) if values else None).rjust(19))
        print(label.ljust(6) + "".join(cells))
    print(f"\nn={len(rows)} cycles on {cycles} requested; all values are milliseconds.")
    print(f"ticket source: {', '.join(sorted(sources)) or 'n/a'}")
    print("detect_ms is 0.0 by construction here (this driver closes the socket itself);")
    print("replay_barrier_ms is n/a because a gateway-side driver has no replay to wait behind.")
    print("Both are filled by the client-side instrumentation in C1.")


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def cmd_ticket(http: Http, args) -> int:
    """Both mint endpoints, timed: what this surface serves and what it refuses."""
    rows = []
    for label, path in TICKET_ENDPOINTS:
        ticket, ms, status = http._mint(path)
        rows.append({"endpoint": label, "status": status, "ms": round(ms, 3),
                     "ticket_chars": len(ticket)})
    used = next((row for row in rows if row["ticket_chars"]), None)
    if args.json:
        print(json.dumps({"endpoints": rows,
                          "used": used["endpoint"] if used else None}, indent=2))
    else:
        for row in rows:
            note = (f"ticket {row['ticket_chars']} chars" if row["ticket_chars"]
                    else f"HTTP {row['status']} — not served on this surface")
            print(f"{row['endpoint']:<34}{row['ms']:9.1f} ms  ({note})")
        print(f"\nticket source for open/phases: {used['endpoint'] if used else 'NONE'}")
    return 0 if used else 1


def cmd_open(http: Http, args) -> int:
    websockets = _load_websockets()
    ticket, mint_ms, source = http.mint_ticket()
    if not ticket:
        print("qa_gateway: the server returned no ticket", file=sys.stderr)
        return 1
    url = http.ws_url(ticket)
    t0 = time.perf_counter()

    async def dial() -> float:
        async with websockets.connect(url, open_timeout=args.timeout,
                                      close_timeout=2, ping_interval=None) as ws:
            elapsed = (time.perf_counter() - t0) * 1000.0
            await ws.close()
            return elapsed

    open_ms = asyncio.run(dial())
    result = {"ticket_ms": round(mint_ms, 3), "ws_open_ms": round(open_ms, 3),
              "ticket_source": source}
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(f"{source} ticket{'':<9}{mint_ms:9.1f} ms")
        print(f"ws  /api/ws (upgrade){open_ms:7.1f} ms")
    return 0


def cmd_phases(http: Http, args) -> int:
    _load_websockets()
    rows: list[dict[str, float | None]] = []
    sources: set[str] = set()
    previous = None
    for _ in range(args.cycles):
        # Each cycle reuses the same view of the server; the close that starts
        # the next cycle is the previous socket's, exactly as a browser's would be.
        current = ProbeSocket(http, args.timeout)
        asyncio.run(current.round(previous))
        rows.append(current.phase)
        sources.add(current.source)
        previous = current
    if args.json:
        summary = {}
        for name in PHASES:
            values = phase_values(rows, name)
            summary[name] = {
                "p50": round(percentile(values, 0.50), 3) if values else None,
                "p95": round(percentile(values, 0.95), 3) if values else None,
                "n": len(values),
            }
        print(json.dumps({"cycles": args.cycles, "rows": rows, "summary": summary,
                          "ticket_sources": sorted(sources)}, indent=2))
    else:
        print_table(rows, args.cycles, sources)
    return 0


class ProbeSocket:
    """One reconnect cycle, holding the live socket so the next cycle closes a real one."""

    def __init__(self, http: Http, timeout: float):
        self._http = http
        self._timeout = timeout
        self.phase: dict[str, float | None] = {name: None for name in PHASES}
        self.source = ""
        self._ws = None

    async def round(self, previous) -> None:
        websockets = _load_websockets()
        # The cycle starts at the close instant. A gateway-side driver closes
        # deliberately, so it knows it instantly: detect_ms is 0.0 here, and only
        # the client (C1) can measure a real detection delay.
        self.phase["detect_ms"] = 0.0
        if previous is not None:
            await previous.close()
        ticket, pre_ipc_ms, self.source = self._http.mint_ticket()
        self.phase["pre_ipc_ms"] = pre_ipc_ms
        if not ticket:
            raise SystemExit("qa_gateway: the server returned no ticket")
        url = self._http.ws_url(ticket)
        t0 = time.perf_counter()
        ws = await asyncio.wait_for(websockets.connect(url, open_timeout=self._timeout,
                                                      close_timeout=2, ping_interval=None),
                                    self._timeout)
        self._ws = ws
        self.phase["dial_ms"] = (time.perf_counter() - t0) * 1000.0
        # ready_ms: the first frame the server offers after the upgrade.
        try:
            await asyncio.wait_for(ws.recv(), READY_BUDGET_S)
            self.phase["ready_ms"] = (time.perf_counter() - t0) * 1000.0
        except asyncio.TimeoutError:
            self.phase["ready_ms"] = None  # nothing unsolicited; the RPC below still measures
        # usable_ms: one real application round trip on the open socket.
        request_id = int(time.time() * 1000) % 1_000_000
        t_rpc = time.perf_counter()
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": request_id,
                                  "method": "client.capabilities",
                                  "params": {"capabilities": {}}}))
        deadline = t_rpc + RPC_BUDGET_S
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), remaining)
            except asyncio.TimeoutError:
                break
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(message, dict) and message.get("id") == request_id:
                self.phase["usable_ms"] = (time.perf_counter() - t_rpc) * 1000.0
                break
        # replay_barrier_ms needs a client with a replay watermark; a gateway-side
        # ticket carries no replay state, so C1's client instrumentation fills it.
        self.phase["replay_barrier_ms"] = None

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass


# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa_gateway.py",
        description="Drive the TEST Hermes (:10119) through the reconnect sequence.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("QA_BASE_URL", DEFAULT_BASE_URL),
                        help=f"test instance base URL (default {DEFAULT_BASE_URL})")
    parser.add_argument("--env-file", default=os.environ.get("QA_ENV_FILE", DEFAULT_ENV_FILE),
                        help="env file holding the dashboard credentials")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-operation timeout, seconds")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("ticket", help="mint a ws ticket and report elapsed ms")
    sub.add_parser("open", help="open /api/ws on a fresh ticket and report elapsed ms")
    phases = sub.add_parser("phases", help="run the reconnect sequence and print the phase table")
    phases.add_argument("--cycles", type=int, default=20, help="reconnect cycles (default 20)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    username, password = credentials(args.env_file)
    http = Http(args.base_url, timeout=args.timeout)
    http.login(username, password)
    if args.command == "ticket":
        return cmd_ticket(http, args)
    if args.command == "open":
        return cmd_open(http, args)
    if args.command == "phases":
        return cmd_phases(http, args)
    raise SystemExit(f"qa_gateway: unknown command {args.command}")


if __name__ == "__main__":
    sys.exit(main())
