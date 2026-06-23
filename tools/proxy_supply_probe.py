"""tools/proxy_supply_probe.py — measure good-proxies.ru REAL live-proxy yield.

Answers empirically: with our key + filters, how many *live* proxies can we
actually get? Tries several config variants (count / max_ping / anon) and reports
fetched -> unique -> live (healthchecked) -> yield% + latency of the live ones.
This is the ground truth for setting TARGET_LIVE / MIN_LIVE realistically.

    python -m tools.proxy_supply_probe
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
load_dotenv(override=True)

from tools.proxy_provider import GoodProxiesProvider

HC_URL = os.getenv("GOODPROXIES_HEALTHCHECK_URL") or "https://www.google.com/generate_204"
HC_TIMEOUT = float(os.getenv("PROBE_HC_TIMEOUT") or "4")


def _probe(p, prov):
    from curl_cffi import requests as r
    t0 = time.perf_counter()
    try:
        resp = r.get(HC_URL, proxies={"http": p, "https": p}, timeout=HC_TIMEOUT)
        if resp.status_code < 500:
            return (p, time.perf_counter() - t0)
    except Exception:
        pass
    return (p, None)


def measure(prov, label, count, max_ping, anon, types):
    prov.count = count
    prov.max_ping = str(max_ping) if max_ping else ""
    prov.anon = anon
    prov.types = types
    t0 = time.perf_counter()
    try:
        raw = prov._fetch_raw()
    except Exception as e:
        print(f"\n[{label}] FETCH FAILED: {str(e)[:160]}")
        return
    fetch_s = time.perf_counter() - t0
    uniq = list(dict.fromkeys(raw))
    t1 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(200, max(1, len(uniq)))) as ex:
        results = list(ex.map(lambda p: _probe(p, prov), uniq))
    hc_s = time.perf_counter() - t1
    live = [(p, lat) for p, lat in results if lat is not None]
    lats = sorted(lat for _, lat in live)
    med = lats[len(lats)//2]*1000 if lats else 0
    p90 = lats[int(len(lats)*0.9)]*1000 if lats else 0
    fast = sum(1 for l in lats if l <= 1.0)  # live AND <1s
    print(f"\n[{label}]  count={count} max_ping={max_ping or '-'} anon={anon} types={types}")
    print(f"    fetched={len(raw)} unique={len(uniq)} (fetch {fetch_s:.1f}s)")
    print(f"    LIVE={len(live)}  yield={100*len(live)/max(1,len(uniq)):.0f}%  (healthcheck {hc_s:.1f}s)")
    print(f"    live latency: median={med:.0f}ms p90={p90:.0f}ms  | live AND <1s: {fast}")


def main():
    prov = GoodProxiesProvider.__new__(GoodProxiesProvider)
    # minimal init without starting the daemon
    import threading
    prov._lock = threading.Lock()
    prov._load_config()
    prov.proxy_countries = {}
    prov.allowed_countries = set()       # don't geo-drop in the probe
    prov.country = ""
    prov.sort_by_latency = True
    prov.min_works = ""
    if not prov.is_enabled():
        print("GOODPROXIES not enabled / no key — set GOODPROXIES_ENABLED + API_KEY in .env")
        return
    print(f"healthcheck target: {HC_URL} (timeout {HC_TIMEOUT}s)")
    # Variant sweep — each is one API call + a concurrent healthcheck.
    measure(prov, "A current",        300,  500, "elite",            "http,https")
    measure(prov, "B wider",         1000,  500, "elite",            "http,https")
    measure(prov, "C looser ping",   1000, 1000, "elite",            "http,https")
    measure(prov, "D looser anon",   1000, 1000, "elite,anonymous",  "http,https")
    measure(prov, "E max",           2000, 1500, "elite,anonymous",  "http,https")


if __name__ == "__main__":
    main()
