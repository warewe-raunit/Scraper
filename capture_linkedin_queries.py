"""
capture_linkedin_queries.py — Capture LinkedIn's modern GraphQL request paths
for EVERY scraper service from a logged-in browser, and persist them as
LINKEDIN_VOYAGER_*_PATH overrides in .env.

Why
---
LinkedIn is migrating its Voyager surface from versioned REST decorations to
GraphQL queryIds that rotate per web-client version. The hardcoded REST paths
still work for profile/company/jobs today but return stubs (posts) or will break
when a decoration is retired. This tool drives the account's own session (DESKTOP
UA — the mobile fingerprint doesn't fire the desktop GraphQL calls) to each
service's page, sniffs the exact GraphQL request, templatizes the variable bits,
verifies it parses, and writes the matching override:

    content  -> LINKEDIN_VOYAGER_CONTENT_PATH      (posts; {keywords}{count}{start})
    people   -> LINKEDIN_VOYAGER_SEARCH_PATH        (people/companies/groups; {keywords}{count}{start}{resultType})
    jobs     -> LINKEDIN_VOYAGER_JOBS_PATH          ({keywords}{location}{count}{start})
    profile  -> LINKEDIN_VOYAGER_PROFILE_PATH       ({public_id})
    company  -> LINKEDIN_VOYAGER_COMPANY_PATH       ({company})

It uses each account's pinned _login_proxy (same IP class as the session) and
falls through accounts/direct if a proxy is dead. Captures are verified against
the response we already have in-memory, so a flaky proxy can't give a false
negative.

Usage
-----
    python capture_linkedin_queries.py [--target all|content|people|jobs|profile|company] [--account acc_li_01] [--headful]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import structlog
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=True)

from playwright.async_api import async_playwright  # noqa: E402
from tools.proxy_config import playwright_proxy_config  # noqa: E402

logger = structlog.get_logger("capture_queries")
SESSIONS_DIR = ROOT / "sessions"
ENV_FILE = ROOT / ".env"

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# Discovery entities (well-known public pages) used to trigger each call.
PROFILE_PUBLIC_ID = "williamhgates"
COMPANY_UNIVERSAL = "microsoft"
SEARCH_KEYWORD = "AI agents"
JOBS_KEYWORD = "software engineer"


def _text(obj):
    seen = 0
    while isinstance(obj, dict) and "text" in obj and seen < 5:
        obj = obj["text"]
        seen += 1
    return obj if isinstance(obj, str) else ""


def _blob(data) -> str:
    try:
        return json.dumps(data)
    except Exception:
        return ""


# ---- per-target detectors: does this response carry hydrated data we want? ----
def _has_commentary(data) -> bool:
    b = _blob(data)
    return bool(re.search(r'"commentary"\s*:\s*\{', b) and re.search(r'"text"\s*:\s*"[^"]{8,}"', b))


def _has_people(data) -> bool:
    for i in (data or {}).get("included", []):
        u = i.get("entityUrn", "") or ""
        nav = i.get("navigationUrl", "") or ""
        if "fsd_profile" in u and ("/in/" in nav or i.get("title")):
            return True
    return False


def _has_jobs(data) -> bool:
    for i in (data or {}).get("included", []):
        if "jobPosting" in (i.get("entityUrn", "") or "").lower() and i.get("title"):
            return True
    return False


def _has_profile(data) -> bool:
    for i in (data or {}).get("included", []):
        if i.get("firstName") is not None and "lastName" in i:
            return True
    return False


def _has_company(data) -> bool:
    for i in (data or {}).get("included", []):
        u = (i.get("entityUrn", "") or "")
        if "fsd_company" in u and (i.get("name") or i.get("universalName")):
            return True
    return False


TARGETS: dict = {
    "content": {
        "env": "LINKEDIN_VOYAGER_CONTENT_PATH",
        "nav": lambda: f"https://www.linkedin.com/search/results/content/?keywords={quote(SEARCH_KEYWORD)}&origin=SWITCH_SEARCH_VERTICAL",
        "detect": _has_commentary,
        # literals to replace with placeholders (longest first)
        "literals": [(SEARCH_KEYWORD, "{keywords}")],
        "templatize_counts": True,
    },
    "people": {
        "env": "LINKEDIN_VOYAGER_SEARCH_PATH",
        "nav": lambda: f"https://www.linkedin.com/search/results/people/?keywords={quote(SEARCH_KEYWORD)}&origin=SWITCH_SEARCH_VERTICAL",
        "detect": _has_people,
        "literals": [(SEARCH_KEYWORD, "{keywords}"), ("PEOPLE", "{resultType}")],
        "templatize_counts": True,
    },
    "jobs": {
        "env": "LINKEDIN_VOYAGER_JOBS_PATH",
        "nav": lambda: f"https://www.linkedin.com/jobs/search/?keywords={quote(JOBS_KEYWORD)}",
        "detect": _has_jobs,
        "literals": [(JOBS_KEYWORD, "{keywords}")],
        "templatize_counts": True,
    },
    "profile": {
        "env": "LINKEDIN_VOYAGER_PROFILE_PATH",
        "nav": lambda: f"https://www.linkedin.com/in/{PROFILE_PUBLIC_ID}/",
        "detect": _has_profile,
        "literals": [(PROFILE_PUBLIC_ID, "{public_id}")],
        "templatize_counts": False,
    },
    "company": {
        "env": "LINKEDIN_VOYAGER_COMPANY_PATH",
        "nav": lambda: f"https://www.linkedin.com/company/{COMPANY_UNIVERSAL}/",
        "detect": _has_company,
        "literals": [(COMPANY_UNIVERSAL, "{company}")],
        "templatize_counts": False,
    },
}


def _login_proxy_for(account_id: str) -> str | None:
    p = SESSIONS_DIR / f"{account_id}__mobile.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("_login_proxy")
    except Exception:
        return None


def _templatize(url: str, literals: list[tuple[str, str]], counts: bool) -> str | None:
    m = re.search(r"https?://[^/]+(/voyager/api/.+)$", url)
    if not m:
        return None
    path = m.group(1)
    for literal, placeholder in literals:
        variants = {literal, quote(literal, safe=""), quote(literal), literal.replace(" ", "+")}
        hit = False
        for v in sorted(variants, key=len, reverse=True):
            if v and v in path:
                path = path.replace(v, placeholder)
                hit = True
        # Identity placeholders ({keywords}/{public_id}/{company}) MUST appear in
        # the captured URL, else the path is hardcoded to one entity (e.g. a
        # URN-based profile/company call) and is useless as a template. Reject it.
        if not hit and placeholder in ("{keywords}", "{public_id}", "{company}"):
            logger.warning("templatize.literal_missing", literal=literal, placeholder=placeholder)
            return None
    if counts:
        path = re.sub(r"(count(?::|%3A))\d+", r"\g<1>{count}", path, count=1)
        path = re.sub(r"(start(?::|%3A))\d+", r"\g<1>{start}", path, count=1)
    return path


def _write_env(updates: dict[str, str]) -> None:
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    remaining = dict(updates)
    out = []
    for ln in lines:
        key = ln.split("=", 1)[0].strip() if "=" in ln else ""
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(ln)
    if remaining:
        if out and out[-1].strip():
            out.append("")
        out.append("# Captured LinkedIn GraphQL paths (capture_linkedin_queries.py)")
        for k, v in remaining.items():
            out.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


async def _verify_in_memory(target: str, data: dict) -> int:
    """Parse the captured response with the real service parser; return count."""
    from api.services.linkedin import LinkedInScraperService
    svc = LinkedInScraperService()
    inc = data.get("included", [])
    if target == "content":
        return len(svc._extract_posts_from_included(inc))
    if target == "profile":
        return 1 if svc._parse_voyager_profile(data).get("name") else 0
    if target == "company":
        return 1 if svc._parse_voyager_company(data).get("name") else 0
    if target in ("people", "jobs"):
        res = svc._parse_search_results(data) if target == "people" else None
        if target == "jobs":
            return len(svc._parse_voyager_jobs(data) or [])
        return sum(len(v) for v in (res or {}).values())
    return 0


async def _run_capture(account_id: str, proxy_url: str | None, targets: list[str],
                       headless: bool, captures: dict, all_calls: list) -> bool:
    session_file = SESSIONS_DIR / f"{account_id}__mobile.json"
    if not session_file.exists():
        return False
    state = json.loads(session_file.read_text(encoding="utf-8"))
    storage = {k: v for k, v in state.items() if not (isinstance(k, str) and k.startswith("_"))}

    logger.info("capture.attempt", account_id=account_id, proxy=proxy_url, targets=targets)
    pw = await async_playwright().start()
    browser = None
    try:
        launch_kwargs: dict = {"headless": headless,
                               "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"]}
        pc = playwright_proxy_config(proxy_url) if (proxy_url and proxy_url != "direct") else None
        if pc:
            launch_kwargs["proxy"] = pc
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(user_agent=DESKTOP_UA,
                                             viewport={"width": 1366, "height": 900},
                                             locale="en-US", storage_state=storage)
        page = await context.new_page()
        pending: list = []

        async def _sniff(resp, active_target: str) -> None:
            url = resp.url
            if "voyager/api" not in url:
                return
            try:
                data = await resp.json()
            except Exception:
                return
            cfg = TARGETS[active_target]
            ok = cfg["detect"](data)
            all_calls.append({"target": active_target, "url": url, "hydrated": ok,
                              "included": len((data or {}).get("included", []))})
            if ok and active_target not in captures:
                captures[active_target] = {"url": url, "data": data, "account": account_id}
                logger.info("capture.hit", target=active_target, url=url[:110])

        for target in targets:
            if target in captures:
                continue
            handler = lambda r, t=target: pending.append(asyncio.ensure_future(_sniff(r, t)))
            page.on("response", handler)
            nav = TARGETS[target]["nav"]()
            logger.info("capture.navigating", target=target, url=nav[:90])
            try:
                await page.goto(nav, wait_until="domcontentloaded", timeout=45_000)
            except Exception as e:
                logger.warning("capture.goto_warn", target=target, error=str(e)[:140])
                if any(x in str(e) for x in ("ERR_TUNNEL", "ERR_PROXY", "ERR_CONNECTION")):
                    return False  # proxy dead → caller tries the next account
            for _ in range(6):
                if target in captures:
                    break
                await asyncio.sleep(2.2)
                try:
                    await page.mouse.wheel(0, 2400)
                except Exception:
                    pass
            page.remove_listener("response", handler)

        if pending:
            try:
                await asyncio.wait(pending, timeout=8)
            except Exception:
                pass
        return any(t in captures for t in targets)
    finally:
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            await pw.stop()
        except Exception:
            pass


async def main() -> int:
    ap = argparse.ArgumentParser(description="Capture LinkedIn GraphQL paths for all services.")
    ap.add_argument("--target", default="all",
                    help="all | content | people | jobs | profile | company (comma-separated ok)")
    ap.add_argument("--account", help="Account to use (default: try all with sessions).")
    ap.add_argument("--headful", action="store_true", help="Show the browser window.")
    args = ap.parse_args()

    if args.target == "all":
        targets = list(TARGETS.keys())
    else:
        targets = [t.strip() for t in args.target.split(",") if t.strip() in TARGETS]
    if not targets:
        print(f"ERROR: no valid --target. Choose from: {', '.join(TARGETS)} or 'all'.")
        return 1

    accounts = ([args.account] if args.account else []) + \
        [p.stem.split("__")[0] for p in sorted(SESSIONS_DIR.glob("acc_li_*__mobile.json"))]
    seen = set()
    accounts = [a for a in accounts if a and not (a in seen or seen.add(a))]
    if not accounts:
        print("ERROR: no acc_li_* session found. Run linkedin_multi_account_login.py first.")
        return 1

    headless = not args.headful and os.getenv("BROWSER_HEADLESS", "true").lower() in ("1", "true", "yes", "on")

    captures: dict = {}
    all_calls: list = []
    logger.info("capture.start", targets=targets, accounts=accounts, headless=headless)

    for account_id in accounts:
        remaining = [t for t in targets if t not in captures]
        if not remaining:
            break
        proxy = _login_proxy_for(account_id) or "direct"
        ok = await _run_capture(account_id, proxy, remaining, headless, captures, all_calls)
        if not ok and proxy != "direct" and not any(c["target"] for c in all_calls if True):
            await _run_capture(account_id, "direct", remaining, headless, captures, all_calls)

    try:
        (ROOT / "_captured_calls.json").write_text(json.dumps(all_calls, indent=2), encoding="utf-8")
    except Exception:
        pass

    if not captures:
        print("\nFAILED: captured nothing. Re-run with --headful and confirm the pages load results.")
        return 2

    # Verify + write each captured target.
    updates: dict[str, str] = {}
    report: list[str] = []
    for target, cap in captures.items():
        cfg = TARGETS[target]
        template = _templatize(cap["url"], cfg["literals"], cfg["templatize_counts"])
        if not template:
            report.append(f"  {target:8s}: captured but could NOT templatize ({cap['url'][:70]}...)")
            continue
        # Dump sample for parser inspection.
        try:
            (ROOT / f"_sample_{target}.json").write_text(json.dumps(cap["data"], indent=2), encoding="utf-8")
        except Exception:
            pass
        n = await _verify_in_memory(target, cap["data"])
        if n <= 0:
            report.append(f"  {target:8s}: captured + templatized but parser got 0 "
                          f"(sample saved _sample_{target}.json) — NOT written")
            continue
        updates[cfg["env"]] = template
        report.append(f"  {target:8s}: OK - {n} parsed -> {cfg['env']}")

    if updates:
        _write_env(updates)

    print("\n==================== CAPTURE SUMMARY ====================")
    for line in report:
        print(line)
    missing = [t for t in targets if t not in captures]
    if missing:
        print(f"  (not captured: {', '.join(missing)} — re-run --headful)")
    if updates:
        print(f"\nWrote {len(updates)} override(s) to {ENV_FILE}. Restart the API server.")
    print("========================================================")
    return 0 if updates else 4


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
