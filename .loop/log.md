# Loop log

2026-06-25T13:41 | harden youtube filter-completeness invariant | PASS | 2 commits (feature + invariant pending)
2026-06-25T13:59 | repo audit + move DB persistence off response path (bg_save) | PASS | 1 commit, 5 files + helper | backlog: 3 candidates queued
2026-06-25T14:14 | triage main.py event-loop-policy backlog item | DEFER | doc-only; prod=py3.10 unaffected, proper fix behavior-risky -> human sign-off
2026-06-25T14:45 | linkedin.py gather review | RESOLVED | already gathers page waves/validation/vetting; failover awaits deliberately sequential — no change
2026-06-25T15:13 | youtube innertube key TTL + refresh-on-rejection | PASS | 1 commit (youtube.py+config+test); backlog cleared
2026-06-25T16:20 | youtube whole-data audit + fix (views/likes/subs numeric, lockup channel_name, channel subcount) | PASS | 1 commit, +15 tests
