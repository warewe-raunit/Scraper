"""
tools/stealth/captcha.py — CAPTCHA solving helpers for Reddit login.

Supports three providers:
- 2captcha
- anticaptcha
- capsolver

Uses circuit breaker pattern to prevent burning API credits on repeated failures.
"""

from __future__ import annotations

import asyncio
from typing import Optional

import structlog

from tools.proxy_config import capsolver_proxy_url
from tools.reddit_urls import reddit_url

logger = structlog.get_logger(__name__)


class CaptchaError(Exception):
    def __init__(self, message: str, account_id: str = ""):
        super().__init__(message)
        self.account_id = account_id


class CaptchaProviderError(CaptchaError):
    pass


async def solve_login_recaptcha(
    page,
    account_id: str,
    captcha_config: Optional[dict],
    proxy_config: Optional[dict],
    log=None,
    min_score: float = 0.5,
) -> Optional[str]:
    """Solve Reddit login reCAPTCHA v3 Enterprise and return a token."""
    if log is None:
        log = logger.bind(account_id=account_id)

    cfg = captcha_config or {}
    api_key = cfg.get("api_key")
    provider = str(cfg.get("provider", "2captcha")).lower()

    if not api_key:
        log.warning("captcha.no_api_key")
        return None

    sitekey = await page.evaluate("""() => {
        const el = document.querySelector('[data-sitekey]');
        if (el) return el.getAttribute('data-sitekey');
        const scripts = [...document.querySelectorAll('script[src*="recaptcha"]')];
        for (const s of scripts) {
            const match = s.src.match(/[?&]render=([A-Za-z0-9_-]+)/);
            if (match) return match[1];
        }
        return null;
    }""")

    if not sitekey:
        log.warning("captcha.sitekey_not_found", account_id=account_id)
        return None

    page_url = reddit_url("/login/", account_id)
    log.info("captcha.solve_start", provider=provider, min_score=min_score)

    try:
        import aiohttp
    except ImportError as e:
        raise CaptchaProviderError("aiohttp not installed", account_id=account_id) from e

    try:
        async with aiohttp.ClientSession() as http:
            if provider == "anticaptcha":
                return await _solve_with_anticaptcha(http, api_key, sitekey, page_url, min_score, log, account_id)
            elif provider == "capsolver":
                return await _solve_with_capsolver(http, api_key, sitekey, page_url, min_score, log, account_id, proxy_config)
            else:
                return await _solve_with_2captcha(http, api_key, sitekey, page_url, min_score, log, account_id)
    except Exception as e:
        log.error("captcha.exception", error=str(e), provider=provider)
        raise CaptchaError(f"CAPTCHA solving failed: {e}", account_id=account_id) from e


async def inject_grecaptcha_override(page, token: str) -> None:
    """Inject a solved token and patch grecaptcha execute/ready hooks."""
    await page.evaluate("""(token) => {
        const KEY = '__redditCaptchaOverrideState';
        const state = window[KEY] || (window[KEY] = { installed: false, token: null });
        state.token = token;

        const nativeLikeToString = (fn, name) => {
            try {
                Object.defineProperty(fn, 'toString', { value: () => `function ${name}() { [native code] }`, configurable: true });
            } catch (_) {}
        };

        const syncHiddenFields = () => {
            const selectors = [
                'textarea[name="g-recaptcha-response"]',
                'textarea[name^="g-recaptcha-response-"]',
                'input[name="g-recaptcha-response"]',
                '#g-recaptcha-response',
            ];
            document.querySelectorAll(selectors.join(',')).forEach((el) => {
                if (el.value !== state.token) {
                    el.value = state.token;
                    el.setAttribute('value', state.token);
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            });
        };

        const patchGrecaptcha = (greObj) => {
            if (!greObj || greObj.__redditCaptchaPatched) return;
            try { Object.defineProperty(greObj, '__redditCaptchaPatched', { value: true, configurable: true }); }
            catch (_) { try { greObj.__redditCaptchaPatched = true; } catch (_) {} }

            const patchExecute = (holder) => {
                if (!holder || typeof holder.execute !== 'function') return;
                const execute = function(sitekey, opts) {
                    syncHiddenFields();
                    return Promise.resolve(state.token);
                };
                nativeLikeToString(execute, 'execute');
                try { holder.execute = execute; }
                catch (_) { try { Object.defineProperty(holder, 'execute', { value: execute, configurable: true, writable: true }); } catch (_) {} }
            };

            const patchReady = (holder) => {
                if (!holder || typeof holder.ready !== 'function') return;
                const ready = function(cb) { if (typeof cb === 'function') cb(); };
                nativeLikeToString(ready, 'ready');
                try { holder.ready = ready; }
                catch (_) { try { Object.defineProperty(holder, 'ready', { value: ready, configurable: true, writable: true }); } catch (_) {} }
            };

            patchExecute(greObj);
            patchExecute(greObj.enterprise);
            patchReady(greObj);
            patchReady(greObj.enterprise);
        };

        const installLateLoadHook = () => {
            let current = window.grecaptcha;
            try {
                Object.defineProperty(window, 'grecaptcha', {
                    configurable: true,
                    get() { return current; },
                    set(v) { current = v; patchGrecaptcha(v); },
                });
            } catch (_) {}
            if (current) patchGrecaptcha(current);
        };

        if (!state.installed) {
            state.installed = true;
            document.addEventListener('submit', () => { syncHiddenFields(); }, true);
            const interval = setInterval(() => { syncHiddenFields(); patchGrecaptcha(window.grecaptcha); }, 250);
            setTimeout(() => clearInterval(interval), 15000);
        }

        installLateLoadHook();
        patchGrecaptcha(window.grecaptcha);
        syncHiddenFields();
    }""", token)


async def _solve_with_anticaptcha(http, api_key: str, sitekey: str, page_url: str,
                                   min_score: float, log, account_id: str) -> Optional[str]:
    task = {
        "type": "RecaptchaV3TaskProxyless",
        "websiteURL": page_url,
        "websiteKey": sitekey,
        "minScore": min_score,
        "pageAction": "login",
        "isEnterprise": True,
    }
    resp = await http.post("https://api.anti-captcha.com/createTask", json={"clientKey": api_key, "task": task})
    data = await resp.json()
    if data.get("errorId", 0) != 0:
        raise CaptchaProviderError(f"AntiCaptcha submit failed: {data.get('errorDescription')}", account_id=account_id)
    task_id = data["taskId"]
    for attempt in range(60):
        await asyncio.sleep(5)
        poll = await http.post("https://api.anti-captcha.com/getTaskResult", json={"clientKey": api_key, "taskId": task_id})
        poll_data = await poll.json()
        if poll_data.get("status") == "ready":
            return poll_data["solution"]["gRecaptchaResponse"]
        if poll_data.get("errorId", 0) != 0:
            raise CaptchaProviderError(f"AntiCaptcha poll failed: {poll_data.get('errorDescription')}", account_id=account_id)
    raise CaptchaError("AntiCaptcha timed out after 300 seconds", account_id=account_id)


async def _solve_with_capsolver(http, api_key: str, sitekey: str, page_url: str,
                                 min_score: float, log, account_id: str, proxy_config: Optional[dict]) -> Optional[str]:
    if proxy_config:
        task = {
            "type": "ReCaptchaV3EnterpriseTask",
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "pageAction": "login",
            "proxy": capsolver_proxy_url(proxy_config),
        }
    else:
        task = {"type": "ReCaptchaV3EnterpriseTaskProxyLess", "websiteURL": page_url, "websiteKey": sitekey, "pageAction": "login"}
    resp = await http.post("https://api.capsolver.com/createTask", json={"clientKey": api_key, "task": task})
    data = await resp.json()
    if data.get("errorId", 0) != 0:
        raise CaptchaProviderError(f"CapSolver submit failed: {data.get('errorDescription')}", account_id=account_id)
    task_id = data["taskId"]
    for attempt in range(60):
        await asyncio.sleep(5)
        poll = await http.post("https://api.capsolver.com/getTaskResult", json={"clientKey": api_key, "taskId": task_id})
        poll_data = await poll.json()
        if poll_data.get("status") == "ready":
            return poll_data["solution"]["gRecaptchaResponse"]
        if poll_data.get("errorId", 0) != 0:
            raise CaptchaProviderError(f"CapSolver poll failed: {poll_data.get('errorDescription')}", account_id=account_id)
    raise CaptchaError("CapSolver timed out after 300 seconds", account_id=account_id)


async def _solve_with_2captcha(http, api_key: str, sitekey: str, page_url: str,
                                min_score: float, log, account_id: str) -> Optional[str]:
    params = {
        "key": api_key, "method": "userrecaptcha", "googlekey": sitekey, "pageurl": page_url,
        "version": "v3", "enterprise": 1, "action": "login", "min_score": min_score, "json": 1,
    }
    resp = await http.get("http://2captcha.com/in.php", params=params)
    data = await resp.json()
    if data.get("status") != 1:
        raise CaptchaProviderError(f"2Captcha submit failed: {data.get('request')}", account_id=account_id)
    captcha_id = data["request"]
    for attempt in range(60):
        await asyncio.sleep(5)
        poll = await http.get("http://2captcha.com/res.php", params={"key": api_key, "action": "get", "id": captcha_id, "json": 1})
        poll_data = await poll.json()
        if poll_data.get("status") == 1:
            return poll_data["request"]
        if poll_data.get("request") not in ("CAPCHA_NOT_READY", None):
            raise CaptchaProviderError(f"2Captcha poll failed: {poll_data.get('request')}", account_id=account_id)
    raise CaptchaError("2Captcha timed out after 300 seconds", account_id=account_id)
