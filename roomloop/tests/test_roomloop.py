"""Tests aimed at what is most likely to be wrong: DST / wall-clock handling (R3, C1),
the R1-vs-R2 tension, overlap edges (R4), non-sequential room ids (R5/C2), series cancel."""
import re
import unittest
from datetime import datetime, timezone

from roomloop.server import dispatch
from roomloop.service import ApiError, RoomLoop

UTC = timezone.utc
NAIVE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")


def make(now=datetime(2027, 1, 1, tzinfo=UTC)):
    clock = [now]
    svc = RoomLoop(now_fn=lambda: clock[0])
    svc.seed_rooms()
    return svc, clock


def utc_hours(svc, sid):
    rows = svc.db.execute("SELECT start_utc FROM bookings WHERE series_id=? ORDER BY start_utc", (sid,))
    return [(r[0] % 86400) // 3600 for r in rows]


class Tests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make()

    def status(self, fn, *a):
        with self.assertRaises(ApiError) as cm:
            fn(*a)
        return cm.exception.status

    # --- C1 / C2 / R5 -------------------------------------------------
    def test_rooms_response_shape_and_non_sequential_ids(self):
        self.assertEqual(self.svc.rooms(), [
            {"id": 3, "name": "Aurora", "capacity": 8}, {"id": 4, "name": "Basalt", "capacity": 4},
            {"id": 9, "name": "Cinder", "capacity": 12}, {"id": 17, "name": "Dune", "capacity": 6}])
        self.assertEqual(dispatch(self.svc, "GET", "/rooms", {}, {})[1], self.svc.rooms())

    def test_availability_covers_every_real_room(self):
        self.svc.create_booking(9, "a", "2027-02-01T09:00:00", "2027-02-01T10:00:00")
        res = {r["id"]: r["available"] for r in self.svc.availability("2027-02-01T09:30:00", "2027-02-01T10:30:00")}
        self.assertEqual(res, {3: True, 4: True, 9: False, 17: True})

    def test_timestamps_are_naive_local_iso(self):
        b = self.svc.create_booking(3, "a", "2027-02-01T09:00:00", "2027-02-01T10:00:00")
        self.assertRegex(b["start"], NAIVE)
        self.assertEqual(b["start"], "2027-02-01T09:00:00")
        self.assertEqual(self.status(self.svc.create_booking, 3, "a", "2027-02-02T09:00:00+01:00", "2027-02-02T10:00:00"), 400)

    # --- R4 -----------------------------------------------------------
    def test_back_to_back_ok_overlap_conflicts(self):
        self.svc.create_booking(3, "a", "2027-02-01T09:00:00", "2027-02-01T10:00:00")
        self.svc.create_booking(3, "b", "2027-02-01T10:00:00", "2027-02-01T11:00:00")   # starts when other ends
        self.svc.create_booking(3, "b", "2027-02-01T08:00:00", "2027-02-01T09:00:00")   # ends when other starts
        self.assertEqual(self.status(self.svc.create_booking, 3, "c", "2027-02-01T09:59:00", "2027-02-01T10:01:00"), 409)
        self.svc.create_booking(4, "c", "2027-02-01T09:00:00", "2027-02-01T10:00:00")   # other room is fine

    # --- R3: same wall-clock time across DST -----------------------------
    def test_denver_keeps_9am_across_us_spring_forward(self):
        r = self.svc.create_recurring(9, "u", "2027-03-08T09:00:00", "2027-03-08T10:00:00", "2027-03-22")
        self.assertEqual([b["start"] for b in r["created"]],
                         ["2027-03-08T09:00:00", "2027-03-15T09:00:00", "2027-03-22T09:00:00"])
        self.assertEqual(utc_hours(self.svc, r["series_id"]), [16, 15, 15])   # MST -> MDT on Mar 14

    def test_berlin_and_denver_switch_on_different_weeks(self):
        r = self.svc.create_recurring(3, "u", "2027-03-22T09:00:00", "2027-03-22T10:00:00", "2027-03-29")
        self.assertEqual(utc_hours(self.svc, r["series_id"]), [8, 7])         # CET -> CEST on Mar 28
        r = self.svc.create_recurring(9, "u", "2027-03-22T09:00:00", "2027-03-22T10:00:00", "2027-03-29")
        self.assertEqual(utc_hours(self.svc, r["series_id"]), [15, 15])       # Denver already switched

    def test_fall_back_keeps_wall_clock(self):
        r = self.svc.create_recurring(9, "u", "2027-10-31T09:00:00", "2027-10-31T10:00:00", "2027-11-14")
        self.assertEqual([b["start"][11:] for b in r["created"]], ["09:00:00"] * 3)
        self.assertEqual(utc_hours(self.svc, r["series_id"]), [15, 16, 16])   # MDT -> MST on Nov 7

    def test_nonexistent_local_time_moves_forward(self):
        b = self.svc.create_booking(3, "u", "2027-03-28T02:30:00", "2027-03-28T04:00:00")   # 02:30 doesn't exist
        self.assertEqual(b["start"], "2027-03-28T03:30:00")

    def test_occurrence_that_collapses_in_gap_is_skipped_not_fatal(self):
        r = self.svc.create_recurring(3, "u", "2027-03-21T02:30:00", "2027-03-21T03:15:00", "2027-04-04")
        self.assertEqual(len(r["created"]), 2)
        self.assertIn("daylight", r["skipped"][0]["reason"])

    def test_ambiguous_local_time_uses_first_occurrence(self):
        self.svc.create_booking(3, "u", "2027-10-31T02:30:00", "2027-10-31T04:00:00")
        first = int(datetime(2027, 10, 31, 0, 30, tzinfo=UTC).timestamp())
        self.assertEqual(self.svc.db.execute("SELECT start_utc FROM bookings").fetchone()[0], first)

    # --- R1 vs R2 -----------------------------------------------------
    def weekly(self):
        return self.svc.create_recurring(9, "u", "2027-02-01T09:00:00", "2027-02-01T10:00:00", "2027-03-22")  # 8 Mondays

    def test_up_to_two_conflicts_are_skipped(self):
        for d in ("08", "15"):
            self.svc.create_booking(9, "x", f"2027-02-{d}T09:30:00", f"2027-02-{d}T10:30:00")
        r = self.weekly()
        self.assertEqual((len(r["created"]), len(r["skipped"])), (6, 2))
        self.assertEqual(self.svc.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 8)

    def test_three_conflicts_save_nothing(self):
        for d in ("08", "15", "22"):
            self.svc.create_booking(9, "x", f"2027-02-{d}T09:30:00", f"2027-02-{d}T10:30:00")
        self.assertEqual(self.status(self.weekly), 409)
        self.assertEqual(self.svc.db.execute("SELECT COUNT(*) FROM bookings").fetchone()[0], 3)
        self.assertEqual(self.svc.db.execute("SELECT COUNT(*) FROM series").fetchone()[0], 0)

    def test_series_where_nothing_can_be_booked_is_rejected(self):
        self.svc.create_booking(9, "x", "2027-02-01T09:30:00", "2027-02-01T10:30:00")
        self.assertEqual(self.status(self.svc.create_recurring, 9, "u", "2027-02-01T09:00:00", "2027-02-01T10:00:00", "2027-02-01"), 409)

    # --- cancel -------------------------------------------------------
    def six_week_series(self):
        return self.svc.create_recurring(3, "u", "2027-01-04T09:00:00", "2027-01-04T10:00:00", "2027-02-08")

    def test_cancel_series_keeps_past_instances(self):
        r = self.six_week_series()
        self.clock[0] = datetime(2027, 1, 19, 12, tzinfo=UTC)     # Jan 4, 11, 18 are now in the past
        out = self.svc.cancel_booking(r["created"][4]["id"])      # cancel via any instance
        self.assertEqual((out["cancelled"], out["kept_past"]), (3, 3))
        left = [b["start"][:10] for b in self.svc.list_bookings()]
        self.assertEqual(left, ["2027-01-04", "2027-01-11", "2027-01-18"])
        self.assertEqual(self.svc.list_series(), [])               # nothing left to clean up

    def test_cancel_single_occurrence_only(self):
        r = self.six_week_series()
        self.svc.cancel_booking(r["created"][2]["id"], "this")
        self.assertEqual(len(self.svc.list_bookings()), 5)
        self.assertEqual(self.svc.list_series()[0]["upcoming"], 5)

    def test_cancelled_slot_can_be_rebooked_and_past_cannot_be_cancelled(self):
        b = self.svc.create_booking(3, "a", "2027-02-01T09:00:00", "2027-02-01T10:00:00")
        self.svc.cancel_booking(b["id"])
        self.svc.create_booking(3, "b", "2027-02-01T09:00:00", "2027-02-01T10:00:00")
        self.clock[0] = datetime(2027, 3, 1, tzinfo=UTC)
        self.assertEqual(self.status(self.svc.cancel_booking, b["id"] + 1), 409)


if __name__ == "__main__":
    unittest.main()
