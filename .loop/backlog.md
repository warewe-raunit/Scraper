# Loop backlog

> **Build rule (ponytail):** every task takes the laziest solution that works —
> stdlib/native before deps, one line before fifty, delete before add. No
> speculative abstractions. Mark deliberate shortcuts with a `# ponytail:`
> comment naming the ceiling. Skipped-vs-add-when noted in the run critique.

## Audit candidates (from repo-wide sweep 2026-06-25)
- [ ] `api/main.py:36` — `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`.
  **DEFERRED — needs human sign-off, do not auto-fix.** Finding (2026-06-25):
  the warning is local-only — production runs Python 3.10 where this API is NOT
  deprecated, so prod is unaffected. The call sets the global *policy* on purpose
  so the uvicorn reloader/parent stays on Selector (WinError 87 guard) while child
  workers use `proactor_loop_factory`; `set_event_loop()` only swaps the current
  thread's loop, not the policy for newly-created loops, so the obvious
  replacement changes Windows behavior. Real fix belongs to whenever the runtime
  moves to 3.12+. Low value, behavior-risky — left for the user.
- [x] Per-method review of `api/services/linkedin.py` for sequential network
  awaits that could `asyncio.gather`. RESOLVED (2026-06-25), no change needed:
  `_paginate_voyager` already gathers page waves; account-validation and proxy
  batch-vetting process gathered results. The remaining sequential awaits
  (`_try(p)` failover loops ~819/866) are deliberate first-working-proxy
  semantics — parallelizing them would waste the proxy pool and multiply load
  per user request. No N+1 latency bug present.
- [ ] `youtube.py` `_get_api_key` builds a fresh `requests.Session()` per attempt
  — confirm whether a reused session would help or hurt proxy rotation. DoD:
  decision recorded; change only if it measurably helps without breaking rotation.

_Append failures or newly discovered work below._
