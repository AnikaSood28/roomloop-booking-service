"""RoomLoop booking core: rooms, single + weekly bookings, cancellation.

Key idea: the API speaks *naive local ISO strings* (constraint C1), but every
booking is also stored as a UTC instant. Conflict checks use the UTC instants,
so they stay correct across DST changes. Recurring bookings are generated from
local date + wall-clock time (never "+7 days of UTC") so 9:00 stays 9:00 (R3).
"""
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ISO = "%Y-%m-%dT%H:%M:%S"
UTC = timezone.utc
OFFICES = {"Berlin": "Europe/Berlin", "Denver": "America/Denver"}
MAX_SKIPS = 2            # R2: "one or two" clashes may be skipped; more => reject all (R1)
MAX_INSTANCES = 104      # two years of weekly bookings
MAX_DURATION = timedelta(hours=24)

SCHEMA = """
CREATE TABLE IF NOT EXISTS rooms(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, capacity INTEGER NOT NULL, office TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS series(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL, user TEXT NOT NULL,
  start_local TEXT NOT NULL, end_local TEXT NOT NULL, until TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bookings(
  id INTEGER PRIMARY KEY AUTOINCREMENT, room_id INTEGER NOT NULL, user TEXT NOT NULL,
  start_local TEXT NOT NULL, end_local TEXT NOT NULL,
  start_utc INTEGER NOT NULL, end_utc INTEGER NOT NULL,
  series_id INTEGER, status TEXT NOT NULL DEFAULT 'active');
CREATE INDEX IF NOT EXISTS ix_bookings_room ON bookings(room_id, start_utc, end_utc);
CREATE INDEX IF NOT EXISTS ix_bookings_series ON bookings(series_id);
"""


class ApiError(Exception):
    def __init__(self, status, message, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def parse_local(value, field):
    try:
        return datetime.strptime(value, ISO)
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be a naive local ISO string like 2026-07-02T09:00:00 (no offset)")


def parse_date(value, field):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ApiError(400, f"{field} must be a date like 2026-07-02")


def resolve(naive, tz):
    """Wall-clock time -> (normalised naive local string, UTC epoch seconds).

    Nonexistent times (spring-forward gap) move forward to the real time;
    ambiguous times (fall-back) use the first occurrence.
    """
    utc = naive.replace(tzinfo=tz).astimezone(UTC)
    return utc.astimezone(tz).strftime(ISO), int(utc.timestamp())


class RoomLoop:
    def __init__(self, path=":memory:", now_fn=None):
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._now = now_fn or (lambda: datetime.now(UTC))

    @contextmanager
    def _tx(self):
        with self._lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def _now_ts(self):
        return int(self._now().timestamp())

    # ---- rooms -------------------------------------------------------
    def add_room(self, room_id, name, capacity, office):
        assert office in OFFICES
        self.db.execute("INSERT OR IGNORE INTO rooms VALUES (?,?,?,?)", (room_id, name, capacity, office))

    def seed_rooms(self):
        # Deliberately NOT sequential (see C2 sample): R5's "1..N" would miss rooms.
        for r in [(3, "Aurora", 8, "Berlin"), (4, "Basalt", 4, "Berlin"),
                  (9, "Cinder", 12, "Denver"), (17, "Dune", 6, "Denver")]:
            self.add_room(*r)

    def rooms(self):
        """Exactly the shape the facilities dashboard already consumes (C2)."""
        rows = self.db.execute("SELECT id, name, capacity FROM rooms ORDER BY id")
        return [dict(r) for r in rows]

    def rooms_details(self):
        rows = self.db.execute("SELECT id, name, capacity, office FROM rooms ORDER BY id")
        return [{**dict(r), "timezone": OFFICES[r["office"]]} for r in rows]

    def _room(self, room_id):
        try:
            rid = int(room_id)
        except (TypeError, ValueError):
            raise ApiError(400, "room_id must be an integer")
        row = self.db.execute("SELECT * FROM rooms WHERE id=?", (rid,)).fetchone()
        if not row:
            raise ApiError(404, f"room {rid} does not exist")
        return row, ZoneInfo(OFFICES[row["office"]])

    # ---- helpers -----------------------------------------------------
    @staticmethod
    def _user(user):
        if not isinstance(user, str) or not user.strip():
            raise ApiError(400, "user is required")
        return user.strip()

    @staticmethod
    def _check_span(s, e):
        if e <= s:
            raise ApiError(422, "end must be after start")
        if e - s > MAX_DURATION:
            raise ApiError(422, "a booking can be at most 24 hours long")

    @staticmethod
    def _slot(tz, s, e):
        """-> (start_local, start_utc, end_local, end_utc) or None if the span collapses (DST gap)."""
        sl, su = resolve(s, tz)
        el, eu = resolve(e, tz)
        return (sl, su, el, eu) if eu > su else None

    def _conflict(self, room_id, s_utc, e_utc):
        # R4: strict inequalities, so back-to-back bookings do not clash.
        row = self.db.execute(
            "SELECT id FROM bookings WHERE room_id=? AND status='active' AND start_utc<? AND end_utc>?",
            (room_id, e_utc, s_utc)).fetchone()
        return row["id"] if row else None

    @staticmethod
    def _dict(r):
        return {"id": r["id"], "room_id": r["room_id"], "user": r["user"], "start": r["start_local"],
                "end": r["end_local"], "series_id": r["series_id"], "status": r["status"]}

    def _get(self, booking_id):
        r = self.db.execute("SELECT * FROM bookings WHERE id=?", (booking_id,)).fetchone()
        if not r:
            raise ApiError(404, f"booking {booking_id} does not exist")
        return r

    # ---- create ------------------------------------------------------
    def create_booking(self, room_id, user, start, end):
        user = self._user(user)
        room, tz = self._room(room_id)
        s, e = parse_local(start, "start"), parse_local(end, "end")
        self._check_span(s, e)
        slot = self._slot(tz, s, e)
        if slot is None:
            raise ApiError(422, "that time does not exist (daylight-saving gap)")
        sl, su, el, eu = slot
        if su <= self._now_ts():
            raise ApiError(422, "start must be in the future")
        with self._tx():
            clash = self._conflict(room["id"], su, eu)
            if clash:
                raise ApiError(409, "room is already booked for that time", conflicting_booking_id=clash)
            cur = self.db.execute(
                "INSERT INTO bookings(room_id,user,start_local,end_local,start_utc,end_utc) VALUES (?,?,?,?,?,?)",
                (room["id"], user, sl, el, su, eu))
            return self._dict(self._get(cur.lastrowid))

    def create_recurring(self, room_id, user, start, end, until, dry_run=False):
        """Weekly series. All-or-nothing (R1) with a small skip allowance (R2)."""
        user = self._user(user)
        room, tz = self._room(room_id)
        s, e = parse_local(start, "start"), parse_local(end, "end")
        self._check_span(s, e)
        until_d = parse_date(until, "until")
        if until_d < s.date():
            raise ApiError(422, "until must not be before the first booking date")
        if (until_d - s.date()).days // 7 + 1 > MAX_INSTANCES:
            raise ApiError(422, f"a series can have at most {MAX_INSTANCES} weekly bookings")
        if resolve(s, tz)[1] <= self._now_ts():
            raise ApiError(422, "the first booking must start in the future")

        span_days = e.date() - s.date()
        slots, skipped, d = [], [], s.date()
        while d <= until_d:                      # R3: rebuild from local date + wall-clock each time
            ns, ne = datetime.combine(d, s.time()), datetime.combine(d + span_days, e.time())
            slot = self._slot(tz, ns, ne)
            if slot is None:
                skipped.append({"start": ns.strftime(ISO), "end": ne.strftime(ISO),
                                "reason": "time does not exist (daylight-saving gap)"})
            else:
                slots.append(slot)
            d += timedelta(days=7)
        total = len(slots) + len(skipped)

        with self._tx():
            keep = []
            for slot in slots:
                clash = self._conflict(room["id"], slot[1], slot[3])
                if clash:
                    skipped.append({"start": slot[0], "end": slot[2], "reason": f"conflicts with booking #{clash}"})
                else:
                    keep.append(slot)
            if not keep or len(skipped) > MAX_SKIPS:
                raise ApiError(409, f"{len(skipped)} of {total} occurrences cannot be booked "
                                    f"(at most {MAX_SKIPS} may be skipped). Nothing was saved.", skipped=skipped)
            if dry_run:   # preview: same checks and same rejection rule, but nothing is written
                return {"series_id": None, "dry_run": True,
                        "would_create": [{"start": sl, "end": el} for sl, _, el, _ in keep],
                        "skipped": sorted(skipped, key=lambda x: x["start"])}
            sid = self.db.execute("INSERT INTO series(room_id,user,start_local,end_local,until) VALUES (?,?,?,?,?)",
                                  (room["id"], user, start, end, until_d.isoformat())).lastrowid
            ids = [self.db.execute(
                "INSERT INTO bookings(room_id,user,start_local,end_local,start_utc,end_utc,series_id) "
                "VALUES (?,?,?,?,?,?,?)", (room["id"], user, sl, el, su, eu, sid)).lastrowid
                for sl, su, el, eu in keep]
            created = [self._dict(self._get(i)) for i in ids]
        return {"series_id": sid, "created": created, "skipped": sorted(skipped, key=lambda x: x["start"])}

    # ---- cancel ------------------------------------------------------
    def cancel_booking(self, booking_id, scope="series"):
        """Instance of a series: scope 'series' (default, per brief) ends all future
        instances; scope 'this' cancels only that one. Past bookings are never touched."""
        if scope not in ("this", "series"):
            raise ApiError(400, "scope must be 'this' or 'series'")
        row = self._get(booking_id)
        if row["series_id"] and scope == "series":
            return self.cancel_series(row["series_id"])
        if row["status"] == "cancelled":
            return {"cancelled": 0, "kept_past": 0, "series_id": row["series_id"]}
        if row["start_utc"] <= self._now_ts():
            raise ApiError(409, "that booking has already started; past bookings are kept")
        with self._tx():
            self.db.execute("UPDATE bookings SET status='cancelled' WHERE id=?", (booking_id,))
        return {"cancelled": 1, "kept_past": 0, "series_id": row["series_id"]}

    def cancel_series(self, series_id):
        if not self.db.execute("SELECT 1 FROM series WHERE id=?", (series_id,)).fetchone():
            raise ApiError(404, f"series {series_id} does not exist")
        now = self._now_ts()
        with self._tx():
            n = self.db.execute("UPDATE bookings SET status='cancelled' "
                                "WHERE series_id=? AND status='active' AND start_utc>?", (series_id, now)).rowcount
            past = self.db.execute("SELECT COUNT(*) c FROM bookings WHERE series_id=? AND status='active' "
                                   "AND start_utc<=?", (series_id, now)).fetchone()["c"]
        return {"cancelled": n, "kept_past": past, "series_id": series_id}

    # ---- queries -----------------------------------------------------
    def list_bookings(self, room_id=None, day_from=None, day_to=None, include_cancelled=False):
        sql, args = "SELECT * FROM bookings WHERE 1=1", []
        if not include_cancelled:
            sql += " AND status='active'"
        if room_id not in (None, ""):
            sql += " AND room_id=?"
            args.append(self._room(room_id)[0]["id"])
        if day_from:
            sql += " AND start_local>=?"
            args.append(parse_date(day_from, "from").isoformat())
        if day_to:
            sql += " AND start_local<?"
            args.append((parse_date(day_to, "to") + timedelta(days=1)).isoformat())
        rows = self.db.execute(sql + " ORDER BY start_local, room_id", args)
        return [self._dict(r) for r in rows]

    def list_series(self):
        """Series that still hold future bookings: the 'room is busy forever' clean-up list."""
        rows = self.db.execute(
            "SELECT s.*, r.name AS room_name, r.office AS office, "
            " SUM(b.status='active' AND b.start_utc>:now) AS upcoming, "
            " SUM(b.status='active' AND b.start_utc<=:now) AS past "
            "FROM series s JOIN rooms r ON r.id=s.room_id LEFT JOIN bookings b ON b.series_id=s.id "
            "GROUP BY s.id HAVING upcoming>0 ORDER BY s.id", {"now": self._now_ts()})
        return [{"id": r["id"], "room_id": r["room_id"], "room_name": r["room_name"], "office": r["office"],
                 "user": r["user"], "weekday": parse_local(r["start_local"], "").strftime("%A"),
                 "time": f'{r["start_local"][11:16]}-{r["end_local"][11:16]}', "until": r["until"],
                 "upcoming": r["upcoming"], "past": r["past"] or 0} for r in rows]

    def availability(self, start, end):
        s, e = parse_local(start, "start"), parse_local(end, "end")
        self._check_span(s, e)
        out = []
        # R5 fix: iterate the rooms that actually exist (ids 3, 4, 9, 17...), never range(1, N+1).
        for row in self.db.execute("SELECT id, name, capacity FROM rooms ORDER BY id").fetchall():
            _, tz = self._room(row["id"])
            slot = self._slot(tz, s, e)
            free = slot is not None and self._conflict(row["id"], slot[1], slot[3]) is None
            out.append({**dict(row), "available": free})
        return out


def seed_demo(svc):
    """Demo data: a one-off booking that deliberately clashes with a later weekly series (R2)."""
    if svc.db.execute("SELECT COUNT(*) c FROM bookings").fetchone()["c"]:
        return
    t = svc._now().date()
    mon = t + timedelta(days=(7 - t.weekday()) % 7 or 7)
    d = lambda n: (mon + timedelta(days=n)).isoformat()
    svc.create_booking(9, "sam", f"{d(14)}T09:30:00", f"{d(14)}T10:30:00")
    svc.create_recurring(9, "priya", f"{d(0)}T09:00:00", f"{d(0)}T10:00:00", d(77))
    svc.create_recurring(4, "lena", f"{d(2)}T14:00:00", f"{d(2)}T15:00:00", d(58))
    svc.create_booking(3, "omar", f"{d(0)}T13:00:00", f"{d(0)}T14:30:00")
    svc.create_booking(17, "ines", f"{d(0)}T10:00:00", f"{d(0)}T11:00:00")
