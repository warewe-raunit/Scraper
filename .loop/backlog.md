# Loop backlog

> **Build rule (ponytail):** every task takes the laziest solution that works —
> stdlib/native before deps, one line before fifty, delete before add. No
> speculative abstractions. Mark deliberate shortcuts with a `# ponytail:`
> comment naming the ceiling. Skipped-vs-add-when noted in the run critique.

## Audit candidates (from repo-wide sweep 2026-06-25)
- [ ] `api/main.py:36` — `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())`
  is deprecated (removal in 3.16) and warns twice on startup. DoD: no
  DeprecationWarning at import, Windows proxy/curl_cffi behavior unchanged,
  tests pass. Needs a Windows-safe replacement (e.g. set on the loop directly),
  not a blind deletion.
- [ ] Per-method review of `api/services/linkedin.py` (2435 lines) for sequential
  network awaits that could `asyncio.gather`. DoD: each multi-fetch loop either
  gathered or annotated why it must be sequential.
- [ ] `youtube.py` `_get_api_key` builds a fresh `requests.Session()` per attempt
  — confirm whether a reused session would help or hurt proxy rotation. DoD:
  decision recorded; change only if it measurably helps without breaking rotation.

_Append failures or newly discovered work below._
