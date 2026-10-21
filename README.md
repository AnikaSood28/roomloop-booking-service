# RoomLoop booking service

Meeting-room booking core with a web dashboard. **Python 3.9+**, no framework. `pip install -r requirements.txt` only installs `tzdata`, which Windows needs for timezones.

```bash
python3 -m roomloop --demo        # http://127.0.0.1:8000  (--demo loads sample bookings, --port/--db available)
python3 -m unittest -v            # 20 tests (rules, DST, concurrency, end-to-end HTTP)
python3 -m roomloop.scenarios     # prints the Denver/Berlin DST test data next to the naive-UTC bug
```

## Why a REST API + dashboard (not a CLI)
Other tools (facilities dashboard, reporting) already talk HTTP/JSON to RoomLoop, so a REST API keeps the
integration surface they expect. The dashboard is a single static page on the same API, so there is one
code path. Stdlib `http.server` + SQLite keeps setup to zero; a real deployment would swap in FastAPI/Postgres.

## API
| Method & path | Purpose |
|---|---|
| `GET /rooms` | Unchanged shape `[{id,name,capacity}]` for the facilities dashboard (ids are **not** 1..N) |
| `GET /rooms/details` | Adds `office`, `timezone` (used by the new dashboard) |
| `GET /bookings?room_id=&from=YYYY-MM-DD&to=YYYY-MM-DD` | Active bookings |
| `POST /bookings` | `{room_id,user,start,end}` |
| `POST /bookings/recurring` | `{room_id,user,start,end,until}` weekly; returns `created` + `skipped`. Add `"dry_run": true` to preview without saving |
| `DELETE /bookings/{id}?scope=series\|this` | Cancel; for a series instance the default (`series`) ends all **future** instances |
| `GET /series`, `DELETE /series/{id}` | List series still holding rooms; end a whole series |
| `GET /availability?start=&end=` | Which rooms are free |

Timestamps are always naive local ISO strings (`2026-07-02T09:00:00`), interpreted in the room's office timezone.

## Layout
`roomloop/service.py` (all rules) · `roomloop/server.py` (HTTP) · `roomloop/static/index.html` (dashboard) ·
`tests/test_roomloop.py` (test data lives in the tests: DST weeks for Berlin vs Denver, conflicts, cancel) ·
`DECISIONS.md`.
