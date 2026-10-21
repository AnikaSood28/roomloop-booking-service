"""Run: python -m roomloop.scenarios

Shows a weekly Monday 09:00 booking across each office's DST change, next to what the
naive "add 7 days of UTC" approach would produce. The naive column reproduces the
Office Manager's "Denver bookings an hour off" complaint; RoomLoop's column does not.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .service import OFFICES, UTC, resolve

CASES = [("Denver", "2027-03-08", 3, "US spring forward is Sun 14 Mar"),
         ("Berlin", "2027-03-22", 3, "EU spring forward is Sun 28 Mar (two weeks later)"),
         ("Berlin", "2027-10-25", 3, "EU fall back is Sun 31 Oct"),
         ("Denver", "2027-11-01", 3, "US fall back is Sun 7 Nov (one week later)")]


def main():
    for office, first, weeks, note in CASES:
        tz = ZoneInfo(OFFICES[office])
        start = datetime.fromisoformat(first + "T09:00:00")
        base = resolve(start, tz)[1]
        print(f"\n{office}, every Monday 09:00 from {first}  ({note})")
        print(f"  {'date':<12}{'RoomLoop local':<16}{'UTC':<8}{'naive +7d UTC':<15}")
        for k in range(weeks):
            local, ts = resolve(start + timedelta(days=7 * k), tz)
            naive = datetime.fromtimestamp(base + k * 7 * 86400, UTC).astimezone(tz)
            flag = "" if f"{naive:%H:%M}" == "09:00" else "   <-- off by an hour"
            print(f"  {local[:10]:<12}{local[11:16]:<16}{datetime.fromtimestamp(ts, UTC):%H:%M}   {naive:%H:%M}{flag}")


if __name__ == "__main__":
    main()
