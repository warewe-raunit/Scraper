"""Cross-worker per-account rate cap in the LinkedIn pool: an account over its
per-minute budget must be skipped at acquire time, not handed out again."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.services.linkedin_account_pool import LinkedInAccountPool, AccountState, ALIVE
from api.services.rate_limiter import AccountRateLimiter


def _bare_pool(accounts, rate_limiter):
    """A pool with just the fields _pick_next_alive_locked touches (skips the
    env/session loading __init__)."""
    pool = object.__new__(LinkedInAccountPool)
    pool._accounts = {a.account_id: a for a in accounts}
    pool._index = 0
    pool.ban_protect_enabled = False
    pool._rate_limiter = rate_limiter
    pool._lease = None  # isolate the rate gate from exclusive leasing
    return pool


def test_exhausted_account_is_skipped():
    acc = AccountState("a", "u", "p", None, status=ALIVE)
    # capacity == max(1, rpm/workers) == 1 token, then throttled.
    pool = _bare_pool([acc], AccountRateLimiter(rpm=1, workers=1))

    assert pool._pick_next_alive_locked() is acc, "first pick consumes the only token"
    assert pool._pick_next_alive_locked() is None, "over budget → no account, not the same one again"


def test_budget_spreads_to_a_second_account():
    a = AccountState("a", "u", "p", None, status=ALIVE)
    b = AccountState("b", "u", "p", None, status=ALIVE)
    pool = _bare_pool([a, b], AccountRateLimiter(rpm=1, workers=1))

    picks = {pool._pick_next_alive_locked().account_id, pool._pick_next_alive_locked().account_id}
    assert picks == {"a", "b"}, "each account's bucket is independent; both serve one"
    assert pool._pick_next_alive_locked() is None, "both now exhausted"


def test_no_limiter_never_gates():
    acc = AccountState("a", "u", "p", None, status=ALIVE)
    pool = _bare_pool([acc], None)
    for _ in range(5):
        assert pool._pick_next_alive_locked() is acc, "disabled limiter = old behavior"


if __name__ == "__main__":
    test_exhausted_account_is_skipped()
    test_budget_spreads_to_a_second_account()
    test_no_limiter_never_gates()
    print("linkedin rate gating self-check passed")
