"""
tests/test_rotation.py — Parity + behavior tests for the shared CooldownPool.

These lock in the exact selection semantics the three former copies had:
round-robin over healthy items, shortest-cooldown fallback when all are
resting, and cooldown preservation across item-set swaps.
"""

import time

from tools.rotation import CooldownPool


def test_round_robin_cycles_in_order():
    pool = CooldownPool(["a", "b", "c"], label="t")
    picks = [pool.get_next() for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]


def test_empty_pool_returns_none():
    assert CooldownPool([], label="t").get_next() is None


def test_cooldown_removes_item_from_rotation():
    pool = CooldownPool(["a", "b"], label="t")
    pool.cool_down("a", 100)
    # Only "b" is healthy, so every pick is "b" while "a" rests.
    assert [pool.get_next() for _ in range(3)] == ["b", "b", "b"]
    assert pool.is_healthy("b") is True
    assert pool.is_healthy("a") is False
    assert 0 < pool.time_remaining("a") <= 100


def test_all_on_cooldown_falls_back_to_shortest():
    pool = CooldownPool(["a", "b", "c"], label="t")
    pool.cool_down("a", 300)
    pool.cool_down("b", 50)   # shortest remaining
    pool.cool_down("c", 200)
    assert pool.get_next() == "b"


def test_no_fallback_returns_none_when_requested():
    pool = CooldownPool(["a"], label="t")
    pool.cool_down("a", 300)
    assert pool.get_next(fallback_to_shortest=False) is None


def test_expired_cooldown_becomes_healthy_again():
    pool = CooldownPool(["a", "b"], label="t")
    pool.cool_down("a", -1)  # already in the past => available now
    assert pool.is_healthy("a") is True
    assert "a" in pool.healthy_items()


def test_clear_lifts_cooldown():
    pool = CooldownPool(["a"], label="t")
    pool.cool_down("a", 300)
    assert pool.is_healthy("a") is False
    pool.clear("a")
    assert pool.is_healthy("a") is True


def test_set_items_preserves_existing_cooldowns():
    pool = CooldownPool(["a", "b"], label="t")
    pool.cool_down("a", 300)
    pool.set_items(["a", "c"])           # b dropped, c added, a survives
    assert set(pool.items) == {"a", "c"}
    assert pool.is_healthy("a") is False  # cooldown preserved across swap
    assert pool.is_healthy("c") is True


def test_candidates_restrict_selection():
    pool = CooldownPool(["a", "b", "c"], label="t")
    picks = [pool.get_next(candidates=["b", "c"]) for _ in range(4)]
    assert picks == ["b", "c", "b", "c"]


def test_cool_down_unknown_item_is_noop():
    pool = CooldownPool(["a"], label="t")
    pool.cool_down("zzz", 300)  # not tracked -> ignored
    assert pool.get_next() == "a"


def test_snapshot_shape():
    pool = CooldownPool(["a"], label="t")
    pool.cool_down("a", 100)
    snap = pool.snapshot()
    assert set(snap["a"].keys()) == {"cooldown_until", "remaining", "healthy"}
    assert snap["a"]["healthy"] is False
