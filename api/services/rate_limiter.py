"""api/services/rate_limiter.py — per-account token-bucket rate limiting.

Reddit limits each account to ~100 requests/minute. Round-robin *spreads* load
across accounts but doesn't *cap* it, so a burst can put several simultaneous
calls on one account and trip a 429 (then cooldown + retry — reactive). This
caps proactively: each account gets a token bucket sized to its per-minute
budget; a request must take a token before it's allowed to use that account. No
token on any account → the caller waits briefly (the existing smart-wait) and
sheds with 503 if the burst outlasts the budget. "Smooth then shed."

Cross-worker coordination without a broker or shared file: under `--workers N`
each worker enforces RPM/N per account, so the sum across workers equals the
real per-account budget. Static partition — a worker can't borrow another's
share — but it's conservative (never exceeds budget) and the load balancer
spreads requests evenly across workers, so the shares stay roughly used.
# ponytail: in-process token bucket, no Redis/Kafka. If traffic is very uneven
# across workers, move to a shared-file bucket with an OS lock; not needed yet.
"""

from __future__ import annotations

import time
from typing import Dict


class _TokenBucket:
    __slots__ = ("capacity", "refill_per_sec", "tokens", "ts")

    def __init__(self, capacity: float, refill_per_sec: float):
        self.capacity = capacity
        self.refill_per_sec = refill_per_sec
        self.tokens = capacity  # start full: an isolated burst passes immediately
        self.ts = time.monotonic()

    def try_consume(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.ts) * self.refill_per_sec)
        self.ts = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def seconds_until_token(self) -> float:
        if self.tokens >= 1.0 or self.refill_per_sec <= 0:
            return 0.0
        return (1.0 - self.tokens) / self.refill_per_sec


class AccountRateLimiter:
    """Per-account token buckets sized to this worker's share of each account's
    per-minute budget. Consumed atomically from the (single-threaded) event loop,
    so no lock is needed."""

    def __init__(self, rpm: float, workers: int = 1):
        per_worker_rpm = rpm / max(1, workers)
        self.capacity = max(1.0, per_worker_rpm)        # ~1 minute of burst headroom
        self.refill_per_sec = per_worker_rpm / 60.0
        self._buckets: Dict[str, _TokenBucket] = {}

    def _bucket(self, account_id: str) -> _TokenBucket:
        b = self._buckets.get(account_id)
        if b is None:
            b = _TokenBucket(self.capacity, self.refill_per_sec)
            self._buckets[account_id] = b
        return b

    def try_consume(self, account_id: str) -> bool:
        """Take one token for the account; False if it's at its rate budget."""
        return self._bucket(account_id).try_consume()

    def soonest_token_seconds(self, account_ids) -> float:
        """Seconds until the soonest of these accounts has a token (0 if any now)."""
        waits = [self._bucket(a).seconds_until_token() for a in account_ids]
        return min(waits) if waits else 0.0


if __name__ == "__main__":
    # Bucket mechanics: capacity 2, refills 10/sec. Drains, throttles, refills.
    b = _TokenBucket(capacity=2, refill_per_sec=10)
    assert b.try_consume() and b.try_consume(), "full bucket allows the burst"
    assert b.try_consume() is False, "empty bucket throttles"
    assert b.seconds_until_token() > 0, "reports wait while empty"
    time.sleep(0.15)  # ~1.5 tokens refilled
    assert b.try_consume() is True, "refilled after waiting"

    # Per-account independence + worker partitioning (capacity == rpm/workers).
    rl = AccountRateLimiter(rpm=100, workers=4)  # 25/min/worker, cap 25
    assert rl.capacity == 25 and abs(rl.refill_per_sec - 25/60) < 1e-9
    for _ in range(25):
        assert rl.try_consume("acc_01"), "25-token burst fits one worker's share"
    assert rl.try_consume("acc_01") is False, "26th throttled this worker"
    assert rl.try_consume("acc_02") is True, "a different account is independent"
    print("rate_limiter self-check passed")
