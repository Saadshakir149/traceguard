"""TraceGuard dashboard: a local web UI over the same investigation workflow.

    python dashboard.py --events data/events.jsonl
    then open http://127.0.0.1:8000

Standard library only. The browser never computes financial state; it only
displays what `investigate()` returns.
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from traceguard.agent import RetryPolicy, investigate
from traceguard.loader import load_jsonl
from traceguard.tools import TOOL_NAMES, EventStore, FaultPlan, InvestigationTools

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 64 * 1024


class App:
    def __init__(self, events_path: str):
        self.events_path = events_path
        self.loaded = load_jsonl(events_path)
        self.store = EventStore(self.loaded)

    def overview(self) -> dict:
        tools = InvestigationTools(self.store)
        orders = []
        for tenant, order in self.store.orders():
            r = investigate(tools, tenant, order, "What is the status of this order?").result
            sevs = {a["severity"] for a in r["anomalies"]}
            worst = next((s for s in ("error", "warning", "info") if s in sevs), None)
            orders.append({"tenant_id": tenant, "order_id": order, "status": r["status"],
                           "confidence": r["confidence"], "anomaly_count": len(r["anomalies"]),
                           "worst_severity": worst,
                           "anomaly_codes": sorted({a["code"] for a in r["anomalies"]})})
        attributed = sum(len(self.store.records_for(t, o)) for t, o in self.store.orders())
        return {
            "events_path": self.events_path,
            "line_count": len(self.loaded.records) + len(self.loaded.bad_lines),
            "record_count": len(self.loaded.records),
            "unattributed_count": len(self.loaded.records) - attributed,
            "bad_lines": [{"line": b.line, "reason": b.reason, "raw": b.raw[:200]}
                          for b in self.loaded.bad_lines],
            "orders": orders,
        }

    def records(self, tenant: str, order: str) -> dict:
        recs = self.store.records_for(tenant, order)
        return {"records": [{"line": r.line, "record": r.data} for r in recs]}

    def investigate(self, body: dict) -> dict:
        tenant, order = body.get("tenant_id"), body.get("order_id")
        if not isinstance(tenant, str) or not isinstance(order, str) or not tenant or not order:
            raise ValueError("tenant_id and order_id are required strings")
        question = body.get("question") if isinstance(body.get("question"), str) else ""
        timeout = _clamp(body.get("tool_timeout"), 0.1, 5.0, 0.5)
        attempts = int(_clamp(body.get("max_attempts"), 1, 10, 3))
        faults = FaultPlan(failures=_faults(body.get("failures")), timeouts=_faults(body.get("timeouts")),
                           hang_seconds=timeout + 0.3)
        tools = InvestigationTools(self.store, faults)
        started = time.perf_counter()
        inv = investigate(tools, tenant, order, question[:500],
                          policy=RetryPolicy(max_attempts=attempts, timeout_seconds=timeout,
                                             backoff_seconds=0.05))
        return {
            "result": inv.result,
            "meta": {
                "tenant_id": tenant,
                "order_id": order,
                "question": question[:500],
                "intent": inv.intent,
                "investigated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "duration_ms": round((time.perf_counter() - started) * 1000),
                "retry_policy": {"max_attempts": attempts, "timeout_seconds": timeout},
            },
            "trace": [{"tool": c.tool, "ok": c.ok, "attempts": c.attempts,
                       "input": c.arguments, "output": c.response} for c in inv.trace],
        }


def _clamp(value, lo, hi, default):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(lo, min(hi, value))


def _faults(value) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {k: int(_clamp(v, 0, 10, 0)) for k, v in value.items() if k in TOOL_NAMES}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quieter console
            sys.stderr.write("[dashboard] " + fmt % args + "\n")

        def _send(self, code: int, payload: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def do_GET(self):
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif url.path == "/api/overview":
                self._json(200, app.overview())
            elif url.path == "/api/records":
                q = parse_qs(url.query)
                self._json(200, app.records(q.get("tenant", [""])[0], q.get("order", [""])[0]))
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if urlparse(self.path).path != "/api/investigate":
                return self._json(404, {"error": "not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length > MAX_BODY:
                    return self._json(413, {"error": "request too large"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                self._json(200, app.investigate(body))
            except (ValueError, json.JSONDecodeError) as exc:
                self._json(400, {"error": str(exc)})

    return Handler


class ExclusiveServer(ThreadingHTTPServer):
    # The stdlib default (SO_REUSEADDR) lets Windows bind a port another
    # program already uses, so the browser silently reaches that program.
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):  # Windows
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_free_port(handler, start: int, tries: int = 20) -> ExclusiveServer:
    for port in range(start, start + tries):
        try:
            return ExclusiveServer(("127.0.0.1", port), handler)
        except OSError:
            print(f"port {port} is busy, trying {port + 1}...", file=sys.stderr)
    raise OSError(f"no free port in {start}-{start + tries - 1}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="TraceGuard dashboard")
    p.add_argument("--events", default=str(Path(__file__).resolve().parent / "data" / "events.jsonl"))
    p.add_argument("--no-browser", action="store_true", help="Do not open a browser tab")
    p.add_argument("--port", type=int, default=8050)
    args = p.parse_args(argv)
    try:
        app = App(args.events)
    except OSError as exc:
        print(f"cannot read events file: {exc}", file=sys.stderr)
        return 2
    try:
        server = bind_free_port(make_handler(app), args.port)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"TraceGuard dashboard on {url}  (Ctrl+C to stop)", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
