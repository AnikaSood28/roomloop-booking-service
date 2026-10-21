"""Tiny REST API (stdlib only) + the dashboard page."""
import argparse
import json
import re
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import ApiError, RoomLoop, seed_demo

INDEX = Path(__file__).parent / "static" / "index.html"


def fields(body, *names):
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    missing = [n for n in names if body.get(n) in (None, "")]
    if missing:
        raise ApiError(400, "missing field(s): " + ", ".join(missing))
    return [body[n] for n in names]


def dispatch(svc, method, path, q, body):
    """Pure routing function -> (status, payload). Easy to test without sockets."""
    one = lambda k: q.get(k, [None])[0]
    if method == "GET":
        if path == "/rooms":
            return 200, svc.rooms()
        if path == "/rooms/details":
            return 200, svc.rooms_details()
        if path == "/bookings":
            return 200, svc.list_bookings(one("room_id"), one("from"), one("to"), one("include_cancelled") == "1")
        if path == "/series":
            return 200, svc.list_series()
        if path == "/availability":
            return 200, svc.availability(one("start"), one("end"))
    elif method == "POST":
        if path == "/bookings":
            return 201, svc.create_booking(*fields(body, "room_id", "user", "start", "end"))
        if path == "/bookings/recurring":
            dry = body.get("dry_run") is True if isinstance(body, dict) else False
            res = svc.create_recurring(*fields(body, "room_id", "user", "start", "end", "until"), dry_run=dry)
            return (200 if dry else 201), res
    elif method == "DELETE":
        m = re.fullmatch(r"/bookings/(\d+)", path)
        if m:
            return 200, svc.cancel_booking(int(m.group(1)), one("scope") or "series")
        m = re.fullmatch(r"/series/(\d+)", path)
        if m:
            return 200, svc.cancel_series(int(m.group(1)))
    raise ApiError(404, "not found")


class Handler(BaseHTTPRequestHandler):
    svc = None

    def log_message(self, *args):
        pass

    def _send(self, status, payload, ctype="application/json"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        if method == "GET" and path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        try:
            body = {}
            if method == "POST":
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}"
                try:
                    body = json.loads(raw)
                except ValueError:
                    raise ApiError(400, "body must be valid JSON")
            status, payload = dispatch(self.svc, method, path, parse_qs(url.query), body)
        except ApiError as e:
            status, payload = e.status, {"error": e.message, **e.extra}
        except Exception:
            traceback.print_exc()
            status, payload = 500, {"error": "internal error"}
        self._send(status, payload)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    ap = argparse.ArgumentParser(description="RoomLoop booking service + dashboard")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="roomloop.db")
    ap.add_argument("--demo", action="store_true", help="load demo bookings (only if the db has none)")
    a = ap.parse_args()
    svc = RoomLoop(a.db)
    svc.seed_rooms()
    if a.demo:
        seed_demo(svc)
    Handler.svc = svc
    print(f"RoomLoop running on http://127.0.0.1:{a.port}")
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
