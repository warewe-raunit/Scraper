"""
tools/proxy_vetting.py — Scrape-time proxy vetting for LinkedIn Voyager calls.

The goodproxies pool is rotating residential with a low instantaneous health
rate (~5%). Trying candidates sequentially against the real Voyager endpoint
burns minutes (40 attempts x 8s). Instead this module probes candidates
CONCURRENTLY against a cheap LinkedIn target and only hands verified-healthy
proxies to the caller — the same trick login-time vetting uses in
tools/browser_manager._vet_rotating_proxy, now available to the scrape path.

Also keeps a process-wide RecentGood sliding-window cache of proxies that
recently completed a request to LinkedIn, so back-to-back scrape calls reuse
known-alive exits instead of redrawing from the (mostly dead) pool.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Iterable, List, Optional, Set
from urllib.parse import urlsplit

import structlog

logger = structlog.get_logger(__name__)

_PROBE_TARGET = "https://www.linkedin.com/robots.txt"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def subnet16(proxy_url: Optional[str]) -> Optional[str]:
    """Return the /16 prefix ("a.b") of a proxy URL's host, or None.

    LinkedIn's session/IP binding behaves class-based rather than exact-IP,
    so proxies sharing a /16 with a known-accepted exit are the best fallback
    candidates when that exit dies.
    """
    if not proxy_url:
        return None
    try:
        host = urlsplit(proxy_url).hostname or ""
        parts = host.split(".")
        if len(parts) == 4 and all(p.isdigit() for p in parts):
            return f"{parts[0]}.{parts[1]}"
    except Exception:
        pass
    return None


async def probe_proxy(proxy_url: str, timeout: float) -> bool:
    """True if the proxy completes an HTTPS round-trip to LinkedIn right now.

    Any HTTP status (even 4xx) proves the CONNECT tunnel + TLS handshake work;
    the only thing we're filtering out here is dead transport.
    """
    try:
        from curl_cffi import requests as ccff
    except Exception:
        return True  # can't probe → let the real request decide
    loop = asyncio.get_running_loop()
    try:
        r = await loop.run_in_executor(
            None,
            lambda: ccff.get(
                _PROBE_TARGET,
                proxies={"http": proxy_url, "https": proxy_url},
                impersonate="chrome120",
                timeout=timeout,
            ),
        )
        return 200 <= r.status_code < 500
    except Exception:
        return False


async def probe_many(proxies: Iterable[str], timeout: float) -> List[str]:
    """Probe concurrently; return the alive subset preserving input order."""
    uniq = list(dict.fromkeys(p for p in proxies if p))
    if not uniq:
        return []
    results = await asyncio.gather(*[probe_proxy(p, timeout) for p in uniq])
    return [p for p, ok in zip(uniq, results) if ok]


class RecentGood:
    """Sliding-window cache of proxies that recently reached LinkedIn."""

    _instance: Optional["RecentGood"] = None

    @classmethod
    def instance(cls) -> "RecentGood":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self.window = _env_int("LINKEDIN_RECENT_PROXY_WINDOW", 300)

    def mark_ok(self, proxy: Optional[str]) -> None:
        if proxy:
            self._seen[proxy] = time.time()

    def mark_bad(self, proxy: Optional[str]) -> None:
        if proxy:
            self._seen.pop(proxy, None)

    def get_recent(self) -> List[str]:
        cutoff = time.time() - self.window
        self._seen = {p: t for p, t in self._seen.items() if t >= cutoff}
        return sorted(self._seen, key=self._seen.get, reverse=True)


async def find_healthy(
    gp,
    want: int = 3,
    prefer_subnets: Iterable[Optional[str]] = (),
    deadline: Optional[float] = None,
    exclude: Iterable[str] = (),
) -> List[str]:
    """Concurrently vet rotating-pool proxies; return up to `want` verified.

    Recently-working proxies are probed first (highest hit rate), then batches
    drawn from the goodproxies pool. Dead candidates go on cooldown so the
    next call doesn't redraw them. Verified results are ordered so subnets in
    `prefer_subnets` come first.

    Tunables (env):
        LINKEDIN_SCRAPE_VET_BATCH    parallel probes per round (default 15)
        LINKEDIN_SCRAPE_VET_TIMEOUT  per-probe timeout seconds (default 4)
        LINKEDIN_SCRAPE_VET_MAX      max candidates probed per call (default 90)
    """
    batch_size = _env_int("LINKEDIN_SCRAPE_VET_BATCH", 15)
    timeout = _env_int("LINKEDIN_SCRAPE_VET_TIMEOUT", 4)
    max_checked = _env_int("LINKEDIN_SCRAPE_VET_MAX", 90)

    recent = RecentGood.instance()
    seen: Set[str] = set(exclude)
    healthy: List[str] = []
    checked = 0

    async def _run_batch(batch: List[str]) -> None:
        nonlocal checked
        checked += len(batch)
        results = await asyncio.gather(*[probe_proxy(p, timeout) for p in batch])
        for p, ok in zip(batch, results):
            if ok:
                healthy.append(p)
                recent.mark_ok(p)
            else:
                recent.mark_bad(p)
                try:
                    gp.mark_failed(p)
                except Exception:
                    pass

    cached = [p for p in recent.get_recent() if p not in seen]
    seen.update(cached)
    if cached:
        await _run_batch(cached[:batch_size])

    while len(healthy) < want and checked < max_checked:
        if deadline is not None and time.time() >= deadline:
            break
        batch: List[str] = []
        for _ in range(batch_size):
            try:
                p = await gp.get_proxy()
            except Exception:
                p = None
            if p and p not in seen:
                seen.add(p)
                batch.append(p)
        if not batch:
            break
        await _run_batch(batch)

    prefer = {s for s in prefer_subnets if s}
    healthy.sort(key=lambda p: 0 if subnet16(p) in prefer else 1)
    logger.info("proxy_vetting.find_healthy_done", checked=checked, found=len(healthy))
    return healthy[:want] if want else healthy
