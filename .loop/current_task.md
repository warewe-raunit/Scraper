## Task
Harden the YouTube search filter-completeness diagnostics by asserting the
count invariant (scanned == kept + dropped_over_cap + dropped_unresolved), and
commit the finished feature onto the loop branch.

## Why this task
First run. Full suite green (106 passed), no `.loop/backlog.md`, no TODOs in
changed files. Scanning surfaced a complete-but-uncommitted feature in
`api/services/youtube.py::search` — a `filter` block reporting `stop_reason`,
`scanned`, `channels_resolved`, `dropped_over_cap`, `dropped_unresolved` so a
"0 results" answer is distinguishable from a ceiling-truncated one. Its
load-bearing invariant — every scanned video lands in exactly one bucket, so
`kept + dropped_over_cap + dropped_unresolved == scanned` — is unverified. A
silent miscount makes the completeness signal lie, defeating the feature.

## Steps
1. Commit the existing youtube.py + test changes onto the loop branch.
2. Add an invariant assertion to
   `test_filter_reports_completeness_and_drop_breakdown`:
   `results_count + dropped_over_cap + dropped_unresolved == scanned`.
3. Run pytest; commit.

## Definition of Done
`tests/test_youtube_subscriber_filter.py` asserts
`results_count + dropped_over_cap + dropped_unresolved == scanned` in the
completeness test, and `python -m pytest -q` passes (106+ tests, 0 failures).

## Risk
The invariant could fail if a video path skips or double-counts a counter — but
that surfaces a real bug, which is the point. Otherwise test-only change plus
committing already-passing code on an isolated branch (never merged). Low risk.
