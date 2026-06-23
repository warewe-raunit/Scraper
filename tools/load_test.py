"""tools/load_test.py — fire N concurrent requests at the running API and report
success rate + latency percentiles, broken down PER ENDPOINT. The evidence for
"can it handle 100 at once" across a realistic mix of paths.

Run against a LIVE instance (start the server first). Pass --path multiple times
to spread the load across endpoints (round-robin); each gets its own stats row so
you can see which platform breaks. --api-key is needed for the /api/v1 paths.

    # single endpoint
    python -m tools.load_test -c 100 -n 300 --path /health

    # realistic mix across platforms
    python -m tools.load_test -c 100 -n 200 --api-key "$KEY" \
        --path "/api/v1/subreddit/python/posts?limit=10" \
        --path "/api/v1/posts/search?q=startup&limit=10" \
        --path "/api/v1/youtube/search?q=python&limit=5" \
        --path "/api/v1/x/profile/elonmusk?limit=5"

# ponytail: stdlib argparse + httpx (already a dep). A semaphore-bounded gather, a
# percentile sort, and a per-path Counter is the whole load test — no locust.
"""

from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter, defaultdict

import httpx


async def _one(client, sem, path, headers):
    async with sem:
        start = time.perf_counter()
        try:
            r = await client.get(path, headers=headers)
            return (path, r.status_code == 200, time.perf_counter() - start, r.status_code)
        except Exception as e:  # connect/timeout/etc — count as failure, keep going
            return (path, False, time.perf_counter() - start, type(e).__name__)


def _pct(vals, p):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))
    return s[i]


def _row(label, oks, total, lats, codes):
    pct = 100 * oks / total if total else 0
    line = (f"  {label:<48} {oks:>3}/{total:<3} ({pct:5.1f}%)  "
            f"p50={_pct(lats,50)*1000:5.0f}ms p95={_pct(lats,95)*1000:6.0f}ms "
            f"max={max(lats)*1000:6.0f}ms" if lats else f"  {label:<48} 0/0")
    if codes:
        line += f"  {dict(codes)}"
    print(line)


async def main():
    ap = argparse.ArgumentParser(description="Concurrent load test for the scraper API.")
    ap.add_argument("-c", "--concurrency", type=int, default=100)
    ap.add_argument("-n", "--requests", type=int, default=200)
    ap.add_argument("--url", default="http://127.0.0.1:8000", help="base URL")
    ap.add_argument("--path", action="append", default=None, help="repeatable; round-robined")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--duration", type=float, default=None,
                    help="if set, hold --concurrency in-flight for this many seconds "
                         "(sustained-throughput mode) instead of sending a fixed --requests count")
    ap.add_argument("--rate", type=float, default=None,
                    help="open-loop: fire this many requests/sec for --duration seconds "
                         "(arrival rate fixed, does NOT wait for completion). The correct "
                         "way to measure a shedding system — a 503 doesn't trigger a re-fire.")
    args = ap.parse_args()

    paths = args.path or ["/health"]
    headers = {"X-API-Key": args.api_key} if args.api_key else {}
    sem = asyncio.Semaphore(args.concurrency)
    limits = httpx.Limits(max_connections=args.concurrency, max_keepalive_connections=args.concurrency)

    if args.rate:
        # Open-loop: fire at a fixed arrival rate, don't wait for completion.
        # 503-sheds don't cause a re-fire (a real client backs off), so this
        # measures true successful throughput under a shedding cap.
        print(f">> open-loop {args.rate} req/s for {args.duration or 30}s, {len(paths)} endpoint(s), {args.url}")
        dur = args.duration or 30.0
        results = []
        tasks = []
        interval = 1.0 / args.rate
        wall = time.perf_counter()
        deadline = wall + dur
        sem_open = asyncio.Semaphore(args.concurrency if args.concurrency else 100000)
        async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout, limits=limits) as client:
            i = 0
            async def fire(path):
                results.append(await _one(client, sem_open, path, headers))
            while time.perf_counter() < deadline:
                tasks.append(asyncio.create_task(fire(paths[i % len(paths)]))); i += 1
                nxt = wall + (i * interval)
                await asyncio.sleep(max(0, nxt - time.perf_counter()))
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        wall = time.perf_counter() - wall
    elif args.duration:
        # Sustained mode: keep `concurrency` requests continuously in flight,
        # round-robining paths, until the deadline — measures steady-state req/s.
        print(f">> sustained {args.duration}s at {args.concurrency} concurrent, {len(paths)} endpoint(s), {args.url}")
        results = []
        counter = {"i": 0}
        wall = time.perf_counter()
        deadline = wall + args.duration
        async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout, limits=limits) as client:
            async def worker():
                while time.perf_counter() < deadline:
                    p = paths[counter["i"] % len(paths)]; counter["i"] += 1
                    results.append(await _one(client, sem, p, headers))
            await asyncio.gather(*(worker() for _ in range(args.concurrency)))
        wall = time.perf_counter() - wall
    else:
        plan = [paths[i % len(paths)] for i in range(args.requests)]
        print(f">> {args.requests} requests, {args.concurrency} concurrent, {len(paths)} endpoint(s), {args.url}")
        wall = time.perf_counter()
        async with httpx.AsyncClient(base_url=args.url, timeout=args.timeout, limits=limits) as client:
            results = await asyncio.gather(*(_one(client, sem, p, headers) for p in plan))
        wall = time.perf_counter() - wall

    # Per-path aggregation
    per = defaultdict(lambda: {"ok": 0, "n": 0, "lat": [], "codes": Counter()})
    for path, ok, dur, code in results:
        d = per[path]
        d["n"] += 1
        d["lat"].append(dur)
        if ok:
            d["ok"] += 1
        else:
            d["codes"][code] += 1

    total_ok = sum(d["ok"] for d in per.values())
    print(f"\nwall {wall:.2f}s | sent {len(results)/wall:.1f} req/s | "
          f"SUCCESSFUL {total_ok/wall:.1f} req/s | "
          f"success {total_ok}/{len(results)} ({100*total_ok/len(results):.1f}%)\n")
    print("per endpoint:")
    for path in sorted(per):
        d = per[path]
        _row(path, d["ok"], d["n"], d["lat"], d["codes"])


if __name__ == "__main__":
    asyncio.run(main())
