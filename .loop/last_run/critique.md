# Self-critique — repo audit + background-save fix

## Hostile-reviewer questions on the change
- **Ordering**: youtube `get_video_details` previously awaited channel-save
  before video-save. Now both are fire-and-forget — could a video FK reference a
  not-yet-saved channel? Checked: saves are independent upserts on separate
  tables, no in-request FK dependency; Supabase upserts are idempotent. Safe.
- **Lost errors**: a failed write no longer fails the response. By design — the
  scrape already succeeded; persistence is best-effort. Failures now log
  `<svc>_bg_db_save_failed` instead of being awaited, so they're still visible.
- **Task GC**: bg tasks held in a module-level set + discarded in the done
  callback — the documented asyncio weak-ref pitfall is handled. Tested.
- **No-loop path**: `save_bg` closes the coro when there's no running loop, so
  sync callers (tests) don't leak. Tested.

## What I did NOT change (deliberately, to avoid behavior drift)
- `api/main.py:36` deprecated `asyncio.set_event_loop_policy` (Python 3.16
  removal) — touching the Windows event-loop policy risks behavior change; queued.
- Deep per-method review of `linkedin.py` (2435 lines) and the `tools/stealth/*`
  fingerprint modules — not network hot paths; queued for the loop.

## Coverage honesty
This was a high-signal pattern sweep (blocking I/O, mutable defaults, N+1
awaits, inline persistence), not a literal line-by-line read of all 25k lines.
The sweep found the codebase already does the hard things right; the one real
systemic latency issue (inline DB await in 3 of 4 services) is fixed. Remaining
files are queued in backlog.md rather than rushed.

## Verdict
No defect in the change. 109 tests pass. DoD met.
