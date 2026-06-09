"""
tools/retry_policy.py — Shared bounded-retry + backoff failover policy.

Why this exists
---------------
Every scraper service (X, YouTube, Reddit) used to hand-roll its own retry loop.
The X service in particular did ``max_attempts = min(3, ...)`` and, after three
proxy failures, returned a ``success: False`` result (or raised) with NO data —
even though the global rotating proxy pool refills every ``GOODPROXIES_REFRESH_
SECONDS`` (default 60s). Burning three proxies from the *current* batch in a few
seconds and then quitting never gives the pool a chance to refill.

This module centralises one robust policy:

  * **Bounded, not infinite.** "Robust" must not mean "retry forever" — a truly
    blocked or down target would hang the request and pile up load. We cap the
    number of attempts AND a wall-clock budget, whichever trips first.
  * **Spans a pool refill.** The default budget/backoff are tuned so the loop
    naturally lives long enough for at least one proxy-pool refresh (≈60s),
    giving rotation fresh IPs to try instead of re-burning a stale batch.
  * **Always returns a structured result.** On exhaustion we return the best
    result we have (or a structured empty/failure dict) — we never propagate a
    bare exception to the caller, so the user always gets *output*.

The policy is transport-agnostic: it knows nothing about HTTP, proxies, or
Playwright. The caller supplies an ``attempt`` coroutine that performs one try
and reports back via :class:`AttemptOutcome`.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class AttemptOutcome:
    """What a single attempt reports back to the retry policy.

    Attributes
    ----------
    success:
        True if the attempt produced usable output. The loop stops immediately
        and returns ``result``.
    result:
        The payload to return to the caller (success OR best-effort failure).
        Carried forward as ``last_result`` between attempts so the final return
        is always the most informative result we saw.
    retryable:
        Whether another attempt could plausibly help. Set False for permanent
        failures (e.g. 404, malformed input) so we stop early instead of
        wasting the whole budget on something that will never succeed.
    cooldown_target:
        Optional opaque token (e.g. a proxy URL) the caller wants rested before
        the next attempt; passed to ``on_cooldown``.
    error:
        Short human-readable reason, used for logging and the exhaustion result.
    """

    success: bool
    result: Any = None
    retryable: bool = True
    cooldown_target: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RetryPolicy:
    """Tunable bounded-retry parameters.

    Defaults are chosen so a run can span at least one ~60s proxy-pool refill:
    up to 8 attempts and up to 90s of wall-clock, with capped exponential
    backoff + jitter between tries.
    """

    max_attempts: int = 8
    time_budget_seconds: float = 90.0
    base_backoff_seconds: float = 1.5
    max_backoff_seconds: float = 12.0
    jitter_seconds: float = 0.75
    label: str = "scrape"

    def backoff_for(self, attempt: int) -> float:
        """Capped exponential backoff (1-indexed attempt) with +jitter."""
        raw = self.base_backoff_seconds * (2 ** (attempt - 1))
        capped = min(self.max_backoff_seconds, raw)
        return capped + random.uniform(0.0, self.jitter_seconds)


async def robust_retry(
    attempt: Callable[[int], Awaitable[AttemptOutcome]],
    *,
    policy: Optional[RetryPolicy] = None,
    on_cooldown: Optional[Callable[[str], Any]] = None,
    exhausted_result: Optional[Callable[[Optional[AttemptOutcome]], Any]] = None,
) -> Any:
    """Run ``attempt`` repeatedly under a bounded retry policy; never raises.

    Parameters
    ----------
    attempt:
        ``async (attempt_number:int) -> AttemptOutcome``. Performs exactly one
        try. MUST NOT raise — wrap your own errors and report them via
        ``AttemptOutcome(success=False, retryable=..., error=...)``. (As a
        safety net we still catch exceptions here and treat them as retryable.)
    policy:
        Retry tuning. Defaults to :class:`RetryPolicy` (spans a 60s refill).
    on_cooldown:
        Optional callback invoked with ``cooldown_target`` after a failed
        attempt, so the caller can rest a bad proxy before the next try.
    exhausted_result:
        Optional factory ``(last_outcome) -> Any`` producing the value returned
        when all attempts/budget are spent. Defaults to returning the last
        outcome's ``result`` (or a generic failure dict if there is none).

    Returns
    -------
    The result of the first successful attempt, or — on exhaustion — the
    best-effort failure result. Always a value, never an exception.
    """
    policy = policy or RetryPolicy()
    deadline = time.monotonic() + policy.time_budget_seconds
    last: Optional[AttemptOutcome] = None

    for n in range(1, policy.max_attempts + 1):
        try:
            outcome = await attempt(n)
        except Exception as e:  # defensive: attempt() should not raise
            logger.error(
                "robust_retry.attempt_raised",
                label=policy.label,
                attempt=n,
                error=str(e),
            )
            outcome = AttemptOutcome(success=False, retryable=True, error=str(e))

        if outcome.success:
            if n > 1:
                logger.info(
                    "robust_retry.recovered", label=policy.label, attempt=n
                )
            return outcome.result

        last = outcome

        # Rest a misbehaving target (e.g. proxy) if the caller asked us to.
        if outcome.cooldown_target and on_cooldown is not None:
            try:
                on_cooldown(outcome.cooldown_target)
            except Exception as e:
                logger.warning(
                    "robust_retry.cooldown_failed",
                    label=policy.label,
                    error=str(e),
                )

        # Permanent failure → stop early, don't burn the whole budget.
        if not outcome.retryable:
            logger.warning(
                "robust_retry.non_retryable_stop",
                label=policy.label,
                attempt=n,
                error=outcome.error,
            )
            break

        # Out of attempts?
        if n >= policy.max_attempts:
            logger.warning(
                "robust_retry.attempts_exhausted",
                label=policy.label,
                attempts=n,
                error=outcome.error,
            )
            break

        # Out of time budget? (check before sleeping)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logger.warning(
                "robust_retry.time_budget_exhausted",
                label=policy.label,
                attempts=n,
                error=outcome.error,
            )
            break

        # Backoff, but never sleep past the deadline.
        sleep_for = min(policy.backoff_for(n), max(0.0, remaining))
        logger.info(
            "robust_retry.retrying",
            label=policy.label,
            attempt=n,
            next_attempt=n + 1,
            sleep_seconds=round(sleep_for, 2),
            error=outcome.error,
        )
        await asyncio.sleep(sleep_for)

    # Exhausted: hand back the best result we have, never an exception.
    if exhausted_result is not None:
        return exhausted_result(last)
    if last is not None and last.result is not None:
        return last.result
    return {
        "success": False,
        "error": (last.error if last else "all retry attempts exhausted"),
        "exhausted": True,
    }
