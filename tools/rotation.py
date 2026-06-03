"""
tools/rotation.py — Shared round-robin selection with per-item cooldown timers.

This is the SINGLE implementation of the "pick the next healthy item, rotate
fairly, and rest items that just failed" logic. It previously existed in three
near-identical copies:

  * api/services/x.py          -> proxy rotation (XScraperService)
  * api/services/youtube.py    -> proxy rotation (YouTubeScraperService)
  * api/services/registry.py   -> account rotation/cooldown (AccountRegistry)

All three now compose `CooldownPool` instead of hand-rolling the same dict +
index + min()-fallback dance. To inspect or debug rotation state, read
`pool.snapshot()` (a plain dict you can log or assert against) rather than
poking private attributes.

Selection semantics (identical to the original three implementations):
  - Among items whose cooldown has expired, pick the next one round-robin.
  - If every candidate is still cooling down, fall back to the item with the
    SHORTEST remaining cooldown (and log a warning).
  - `get_next` returns None only when there are no items at all.
"""

from __future__ import annotations

import time
import threading
from typing import Callable, Dict, Iterable, List, Optional

import structlog

logger = structlog.get_logger(__name__)


def _default_redact(item: str) -> str:
    """Truncate long/secret-bearing identifiers (e.g. proxy URLs) for logs."""
    s = str(item)
    return (s[:30] + "...") if len(s) > 30 else s


class CooldownPool:
    """
    A fair round-robin pool of items, each with an independent cooldown timer.

    Parameters
    ----------
    items:
        Initial iterable of hashable item identifiers (proxy URLs, account ids).
        ``None`` entries are ignored; duplicates are collapsed.
    label:
        Human-readable name used in structured logs (e.g. ``"x_proxy"``,
        ``"reddit_account"``). Lets you tell pools apart in one log stream.
    default_cooldown:
        Cooldown duration in seconds applied when ``cool_down`` is called
        without an explicit duration.
    redact:
        Optional callable to sanitize an item for logging. Defaults to
        truncating anything longer than 30 chars (good for proxy URLs that
        embed credentials). Pass ``str`` to log identifiers verbatim.
    """

    def __init__(
        self,
        items: Optional[Iterable[str]] = None,
        *,
        label: str = "item",
        default_cooldown: float = 300.0,
        redact: Optional[Callable[[str], str]] = None,
    ) -> None:
        self.label = label
        self.default_cooldown = float(default_cooldown)
        self._redact = redact or _default_redact
        # item -> unix timestamp until which it is resting (0.0 == available now)
        self._cooldowns: Dict[str, float] = {}
        self._index = 0
        self._lock = threading.Lock()
        if items:
            self.set_items(items)

    # --------------------------------------------------------------- membership
    def set_items(self, items: Iterable[str]) -> None:
        """
        Replace the tracked items, preserving the cooldown of any item that
        survives the swap (matches the original ``proxies`` setter behavior).
        """
        with self._lock:
            new: Dict[str, float] = {}
            for it in items:
                if it is None:
                    continue
                new[it] = self._cooldowns.get(it, 0.0)
            self._cooldowns = new

    def add(self, item: str) -> None:
        """Register a single item if not already present (no-op otherwise)."""
        if item is None:
            return
        with self._lock:
            self._cooldowns.setdefault(item, 0.0)

    @property
    def items(self) -> List[str]:
        """All tracked items, in insertion order."""
        return list(self._cooldowns.keys())

    def __len__(self) -> int:
        return len(self._cooldowns)

    def __contains__(self, item: object) -> bool:
        return item in self._cooldowns

    # ------------------------------------------------------------------- health
    def is_healthy(self, item: str, now: Optional[float] = None) -> bool:
        """True if the item is unknown-or-available (cooldown has expired)."""
        now = time.time() if now is None else now
        return self._cooldowns.get(item, 0.0) <= now

    def time_remaining(self, item: str, now: Optional[float] = None) -> float:
        """Seconds until the item becomes available (0.0 if already available)."""
        now = time.time() if now is None else now
        return max(0.0, self._cooldowns.get(item, 0.0) - now)

    def healthy_items(self, now: Optional[float] = None) -> List[str]:
        """Subset of items whose cooldown has expired."""
        now = time.time() if now is None else now
        return [it for it, until in self._cooldowns.items() if until <= now]

    # ----------------------------------------------------------------- mutation
    def cool_down(self, item: str, duration_seconds: Optional[float] = None) -> None:
        """
        Rest an item for ``duration_seconds`` (or ``default_cooldown``).
        Unknown items are ignored, matching the original guarded behavior.
        """
        if item not in self._cooldowns:
            return
        dur = self.default_cooldown if duration_seconds is None else float(duration_seconds)
        until = time.time() + dur
        with self._lock:
            self._cooldowns[item] = until
        logger.warning(
            "cooldown_activated",
            pool=self.label,
            item=self._redact(item),
            duration_seconds=dur,
            until=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(until)),
        )

    def clear(self, item: str) -> None:
        """Mark an item immediately available again (e.g. after a re-login)."""
        with self._lock:
            if item in self._cooldowns:
                self._cooldowns[item] = 0.0

    # ---------------------------------------------------------------- selection
    def get_next(
        self,
        candidates: Optional[Iterable[str]] = None,
        *,
        fallback_to_shortest: bool = True,
    ) -> Optional[str]:
        """
        Return the next item to use.

        candidates:
            If given, restrict selection to these (already-known) items. Unknown
            candidates are ignored. Used by AccountRegistry to rotate only over
            the subset it considers healthy (cooldown + ratelimit + relogin).
        fallback_to_shortest:
            When no candidate is currently available: if True, return the item
            with the shortest remaining cooldown (X/YouTube proxy behavior); if
            False, return None so the caller can run its own exhausted-pool path
            (AccountRegistry raises a descriptive error instead).

        Returns None when there are no items at all, or when nothing is
        available and ``fallback_to_shortest`` is False.
        """
        with self._lock:
            if candidates is None:
                pool = list(self._cooldowns.keys())
            else:
                pool = [c for c in candidates if c in self._cooldowns]
            if not pool:
                return None

            now = time.time()
            healthy = [it for it in pool if self._cooldowns[it] <= now]

            if not healthy:
                if not fallback_to_shortest:
                    return None
                best = min(pool, key=lambda it: self._cooldowns[it])
                logger.warning(
                    "all_on_cooldown_falling_back",
                    pool=self.label,
                    fallback=self._redact(best),
                    wait_seconds=round(self._cooldowns[best] - now, 1),
                )
                return best

            chosen = healthy[self._index % len(healthy)]
            self._index += 1
            return chosen

    # --------------------------------------------------------------- inspection
    def snapshot(self, now: Optional[float] = None) -> Dict[str, Dict[str, float]]:
        """
        Debug-friendly view of the pool: ``{item: {"cooldown_until", "remaining",
        "healthy"}}``. Log or assert against this instead of reading internals.
        """
        now = time.time() if now is None else now
        return {
            it: {
                "cooldown_until": until,
                "remaining": round(max(0.0, until - now), 1),
                "healthy": until <= now,
            }
            for it, until in self._cooldowns.items()
        }
