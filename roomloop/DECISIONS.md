# Decisions

**(a) Decisions where the brief was silent**
- **R1 vs R2 conflict:** recurring creation runs in one transaction. Up to **2** clashing instances are skipped and reported back; **3+** clashes, or nothing bookable at all, rejects the whole request (409) and saves nothing. Skipped dates are always shown to the user.
- **R5 vs C2:** the sample `/rooms` has ids 3, 4, 9, 17, so "iterate 1..N" would miss rooms. Availability iterates the rooms that actually exist. `GET /rooms` keeps its exact shape; office/timezone are on a separate `/rooms/details`.
- **Timezones (R3 + C1):** each room belongs to an office (Berlin/Denver → IANA zone). API keeps naive local strings; internally every booking also stores a UTC instant and conflicts are checked on that. Occurrences are built from local date + wall-clock time, never "+7 days in UTC", so 9:00 stays 9:00 across DST (tests cover both offices' different switch weeks). I suspect Denver's "an hour off" came from fixed-offset or UTC arithmetic, but I can't confirm that from the brief.
- **DST edge cases:** a nonexistent local time (e.g. 02:30 on spring-forward) moves forward to the real time; an ambiguous time (fall-back) uses the first occurrence; an occurrence that collapses to zero length is skipped and counts toward the skip limit.
- **Cancel:** soft-cancel (status flag) so history stays. Cancelling any instance of a series ends all *future* instances (per brief); `scope=this` cancels one. "Future" = starts after now. Series with upcoming bookings are listed on the dashboard with an "End series" button (Office Manager's request).
- **Preview before committing:** `dry_run` on the recurring endpoint (and a Preview button) runs the same checks and the same reject rule but saves nothing, so users see skipped dates *before* anything is booked.
- **Limits I chose:** max 24h per booking, max 104 weekly instances, start must be in the future.

**(b) Questions for the PM**
- Is "one or two" skips a hard limit of 2, or a percentage? Is the preview enough, or must the user explicitly confirm skips?
- Who may cancel a series: only the owner, or anyone/admins (the Office Manager wants easy clean-up)? We have no auth yet.
- Can a room ever move offices, and are there bookings by people travelling between offices?
- Should the series auto-expire or send "still needed?" reminders to stop zombie bookings?

**(c) Where AI helped, and what it got wrong**
- I used AI to help understand the requirements, think through edge cases, and come up with test scenarios. It was especially useful for the recurring booking and timezone logic, where a few small mistakes could cause unexpected behavior. 
 While working through the implementation, I paid attention to a few important details:

 * Booking conflicts: Two bookings that end and start at exactly the same time should be allowed. The overlap check needs strict inequalities to handle this correctly.

 * Recurring bookings: I made sure that skipped dates are reported, rejected requests don't leave partial bookings, and previewing a recurring booking doesn't save anything.

I didn't rely on AI suggestions blindly. I checked the logic against the requirements and used the tests to verify the important cases. AI helped speed up the process, but I made the final implementation decisions and am responsible for the behavior of the service.



**(d) Left out / next steps**
- Auth and ownership, editing a booking, "this and following" scope, pagination, room management.
- Multi-process concurrency (SQLite + a process lock is fine for one server; Postgres exclusion constraints would be the real fix).
- Day view assumes 07:00–20:00; dashboard has no automated UI tests.
