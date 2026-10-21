"""Preview, concurrency and end-to-end HTTP tests."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from roomloop.server import Handler
from roomloop.service import ApiError
from tests.test_roomloop import make


class PreviewTests(unittest.TestCase):
    def test_preview_saves_nothing_and_matches_real_outcome(self):
        svc, _ = make()
        svc.create_booking(9, "x", "2027-02-08T09:30:00", "2027-02-08T10:30:00")
        args = (9, "u", "2027-02-01T09:00:00", "2027-02-01T10:00:00", "2027-03-22")
        prev = svc.create_recurring(*args, dry_run=True)
        self.assertEqual((len(prev["would_create"]), len(prev["skipped"])), (7, 1))
        self.assertEqual(svc.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)
        self.assertEqual(svc.db.execute("SELECT COUNT(*) FROM series").fetchone()[0], 0)
        real = svc.create_recurring(*args)
        self.assertEqual(len(real["created"]), len(prev["would_create"]))

    def test_preview_rejects_when_real_request_would(self):
        svc, _ = make()
        for d in ("08", "15", "22"):
            svc.create_booking(9, "x", f"2027-02-{d}T09:30:00", f"2027-02-{d}T10:30:00")
        with self.assertRaises(ApiError) as cm:
            svc.create_recurring(9, "u", "2027-02-01T09:00:00", "2027-02-01T10:00:00", "2027-03-22", dry_run=True)
        self.assertEqual(cm.exception.status, 409)


class ConcurrencyTests(unittest.TestCase):
    def test_only_one_of_many_simultaneous_bookings_wins(self):
        svc, _ = make()
        results = []

        def attempt(i):
            try:
                svc.create_booking(3, f"u{i}", "2027-02-01T09:00:00", "2027-02-01T10:00:00")
                results.append(201)
            except ApiError as e:
                results.append(e.status)
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(12)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [201] + [409] * 11)
        self.assertEqual(svc.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 1)


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.svc, _ = make()
        Handler.svc = self.svc
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def call(self, method, path, body=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_full_flow_over_http(self):
        st, rooms = self.call("GET", "/rooms")
        self.assertEqual((st, [r["id"] for r in rooms], set(rooms[0])), (200, [3, 4, 9, 17], {"id", "name", "capacity"}))
        slot = {"room_id": 3, "user": "a", "start": "2027-02-01T09:00:00", "end": "2027-02-01T10:00:00"}
        self.assertEqual(self.call("POST", "/bookings", slot)[0], 201)
        self.assertEqual(self.call("POST", "/bookings", slot)[0], 409)
        self.assertEqual(self.call("POST", "/bookings", {"room_id": 3})[0], 400)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        weekly = {**slot, "room_id": 9, "until": "2027-02-22"}
        st, prev = self.call("POST", "/bookings/recurring", {**weekly, "dry_run": True})
        self.assertEqual((st, len(prev["would_create"])), (200, 4))
        st, made = self.call("POST", "/bookings/recurring", weekly)
        self.assertEqual((st, len(made["created"])), (201, 4))
        st, out = self.call("DELETE", f"/series/{made['series_id']}")
        self.assertEqual((st, out["cancelled"]), (200, 4))


if __name__ == "__main__":
    unittest.main()
