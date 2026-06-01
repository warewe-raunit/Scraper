"""
tools/stealth/fingerprint.py — BrowserProfileManager

Manages per-client browser profiles for consistent browser environments.
Each client gets a unique, deterministic browser profile that persists
across sessions. Profiles are injected via page.addInitScript() before
any navigation occurs.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any

from playwright.async_api import Page

# ---------------------------------------------------------------------------
# Reference pools — sourced from real-world browser telemetry
# ---------------------------------------------------------------------------

_WEBGL_RENDERERS: list[str] = [
    "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (Intel, Intel(R) UHD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "ANGLE (AMD, AMD Radeon RX 5700 XT Direct3D11 vs_5_0 ps_5_0, D3D11)",
]

_SCREEN_RESOLUTIONS: list[tuple[int, int]] = [
    (1920, 1080),
    (1536, 864),
    (1440, 900),
    (1366, 768),
]

_PLUGIN_POOL: list[dict[str, str]] = [
    {"name": "PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Chrome PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Chromium PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Microsoft Edge PDF Viewer", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "WebKit built-in PDF", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Chrome PDF Plugin", "filename": "internal-pdf-viewer", "description": "Portable Document Format"},
    {"name": "Native Client", "filename": "internal-nacl-plugin", "description": ""},
    {"name": "Widevine Content Decryption Module", "filename": "widevinecdmadapter.dll", "description": ""},
]

_OPTIONAL_FONTS: list[str] = [
    "Cambria", "Constantia", "Lucida Bright", "Palatino Linotype",
    "Book Antiqua", "Garamond", "Century Gothic", "Calibri Light",
    "Candara", "Franklin Gothic Medium",
]

_BASE_FONTS: list[str] = [
    "Arial", "Arial Black", "Comic Sans MS", "Courier New", "Georgia",
    "Impact", "Microsoft Sans Serif", "Segoe UI", "Tahoma",
    "Times New Roman", "Trebuchet MS", "Verdana",
]

_USER_AGENT_TEMPLATES: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
]

_SEC_CH_UA_MAP: dict[str, str] = {
    "148": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "147": '"Not_A Brand";v="8", "Chromium";v="147", "Google Chrome";v="147"',
    "136": '"Google Chrome";v="136", "Chromium";v="136", "Not.A/Brand";v="24"',
    "135": '"Google Chrome";v="135", "Chromium";v="135", "Not.A/Brand";v="24"',
    "134": '"Google Chrome";v="134", "Chromium";v="134", "Not:A-Brand";v="24"',
    "133": '"Google Chrome";v="133", "Chromium";v="133", "Not?A_Brand";v="24"',
}

_DEFAULT_SEC_CH_UA = _SEC_CH_UA_MAP["133"]

_DEFAULT_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Mobile Safari/537.36"
)
_DEFAULT_MOBILE_SEC_CH_UA = (
    _SEC_CH_UA_MAP["147"]
)

_MOBILE_DEVICE_PROFILES: list[dict[str, Any]] = [
    {
        "id": "pixel_7",
        "brand": "Google",
        "model": "Pixel 7",
        "android": "13",
        "viewports": [(412, 915, 2.625), (393, 873, 2.75), (432, 960, 2.5), (412, 892, 2.625)],
        "hardware_concurrency": [4, 8],
        "device_memory": [8, 8, 6],
        "fonts_index": 2,
        "webgl_renderer": "ANGLE (ARM, Mali-G710, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (ARM)",
    },
    {
        "id": "samsung_galaxy_s23",
        "brand": "Samsung",
        "model": "SM-S911B",
        "android": "13",
        "viewports": [(360, 780, 3.0), (384, 832, 2.8125), (412, 892, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [8],
        "fonts_index": 0,
        "webgl_renderer": "ANGLE (ARM, Adreno 740, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "samsung_galaxy_s22",
        "brand": "Samsung",
        "model": "SM-S901B",
        "android": "13",
        "viewports": [(360, 772, 3.0), (384, 824, 2.8125), (412, 883, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [8],
        "fonts_index": 0,
        "webgl_renderer": "ANGLE (ARM, Adreno 730, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "samsung_galaxy_a54",
        "brand": "Samsung",
        "model": "SM-A546B",
        "android": "13",
        "viewports": [(412, 915, 2.625), (384, 854, 2.75), (360, 800, 3.0)],
        "hardware_concurrency": [8],
        "device_memory": [6, 8],
        "fonts_index": 0,
        "webgl_renderer": "ANGLE (ARM, Mali-G68, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (ARM)",
    },
    {
        "id": "oneplus_11",
        "brand": "OnePlus",
        "model": "CPH2449",
        "android": "13",
        "viewports": [(412, 919, 2.625), (384, 854, 2.75), (360, 800, 3.0)],
        "hardware_concurrency": [8],
        "device_memory": [8, 12],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Adreno 740, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "xiaomi_13",
        "brand": "Xiaomi",
        "model": "2211133G",
        "android": "13",
        "viewports": [(393, 873, 2.75), (412, 915, 2.625), (360, 800, 3.0)],
        "hardware_concurrency": [8],
        "device_memory": [8, 12],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Adreno 740, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "motorola_edge_40",
        "brand": "Motorola",
        "model": "moto edge 40",
        "android": "13",
        "viewports": [(393, 873, 2.75), (412, 915, 2.625), (360, 800, 3.0)],
        "hardware_concurrency": [8],
        "device_memory": [8],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Mali-G610, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (ARM)",
    },
    {
        "id": "pixel_8",
        "brand": "Google",
        "model": "Pixel 8",
        "android": "14",
        "viewports": [(412, 915, 2.625), (393, 873, 2.75), (412, 892, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [8],
        "fonts_index": 2,
        "webgl_renderer": "ANGLE (ARM, Mali-G715, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (ARM)",
    },
    {
        "id": "samsung_galaxy_s24",
        "brand": "Samsung",
        "model": "SM-S921B",
        "android": "14",
        "viewports": [(360, 780, 3.0), (384, 832, 2.8125), (412, 892, 2.625)],
        "hardware_concurrency": [8, 10],
        "device_memory": [8, 12],
        "fonts_index": 0,
        "webgl_renderer": "ANGLE (ARM, Xclipse 940, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Samsung)",
    },
    {
        "id": "oneplus_12",
        "brand": "OnePlus",
        "model": "CPH2581",
        "android": "14",
        "viewports": [(450, 960, 3.0), (412, 919, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [12, 16],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Adreno 750, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "nothing_phone_2",
        "brand": "Nothing",
        "model": "A065",
        "android": "13",
        "viewports": [(393, 873, 2.75), (412, 915, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [8, 12],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Adreno 730, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "sony_xperia_1_v",
        "brand": "Sony",
        "model": "XQ-DQ72",
        "android": "13",
        "viewports": [(384, 864, 2.8125), (412, 927, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [12],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Adreno 740, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
    {
        "id": "google_pixel_fold",
        "brand": "Google",
        "model": "Pixel Fold",
        "android": "13",
        "viewports": [(379, 842, 2.875), (412, 915, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [12],
        "fonts_index": 2,
        "webgl_renderer": "ANGLE (ARM, Mali-G710, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (ARM)",
    },
    {
        "id": "asus_zenfone_10",
        "brand": "Asus",
        "model": "AI2302",
        "android": "13",
        "viewports": [(360, 800, 3.0), (412, 915, 2.625)],
        "hardware_concurrency": [8],
        "device_memory": [8, 16],
        "fonts_index": 1,
        "webgl_renderer": "ANGLE (ARM, Adreno 740, OpenGL ES 3.2)",
        "webgl_vendor": "Google Inc. (Qualcomm)",
    },
]

_ANDROID_FONT_POOLS: list[list[str]] = [
    ["Roboto", "Noto Sans", "Noto Color Emoji", "Arial"],
    ["Roboto", "Noto Sans", "Noto Serif", "Noto Color Emoji", "Arial"],
    ["Roboto", "Google Sans", "Noto Sans", "Noto Color Emoji", "Arial"],
]

_PERMISSION_BASE_STATES: dict[str, str] = {
    "notifications": "prompt",
    "push": "prompt",
    "midi": "prompt",
    "camera": "prompt",
    "microphone": "prompt",
    "speaker-selection": "prompt",
    "device-info": "granted",
    "background-sync": "granted",
    "bluetooth": "prompt",
    "persistent-storage": "prompt",
    "ambient-light-sensor": "prompt",
    "accelerometer": "prompt",
    "gyroscope": "prompt",
    "magnetometer": "prompt",
    "clipboard-read": "prompt",
    "clipboard-write": "granted",
    "payment-handler": "granted",
    "idle-detection": "prompt",
    "periodic-background-sync": "prompt",
    "screen-wake-lock": "prompt",
    "nfc": "prompt",
    "geolocation": "prompt",
    "window-management": "prompt",
    "window-placement": "prompt",
    "storage-access": "prompt",
    "display-capture": "prompt",
}

_PERMISSION_VARIANT_KEYS: tuple[str, ...] = (
    "notifications",
    "camera",
    "microphone",
    "geolocation",
    "persistent-storage",
    "clipboard-read",
)

_COLLISION_KEYS: list[str] = [
    "webgl_renderer", "screen_resolution", "hardware_concurrency",
    "device_memory", "canvas_noise_seed",
]

_TIMEZONE_LOCALE_MAP: dict[str, str] = {
    "America/New_York": "en-US",
    "America/Chicago": "en-US",
    "America/Denver": "en-US",
    "America/Los_Angeles": "en-US",
    "America/Phoenix": "en-US",
    "America/Anchorage": "en-US",
    "Pacific/Honolulu": "en-US",
    "America/Toronto": "en-CA",
    "Europe/London": "en-GB",
    "Europe/Berlin": "de-DE",
    "Europe/Paris": "fr-FR",
    "Asia/Tokyo": "ja-JP",
    "Asia/Seoul": "ko-KR",
    "Australia/Sydney": "en-AU",
}


def _deterministic_int(account_id: str, salt: str) -> int:
    digest = hashlib.sha256(f"{account_id}:{salt}".encode()).hexdigest()
    return int(digest[:16], 16)


def _pick_from_pool(pool: list[Any], account_id: str, salt: str) -> Any:
    return pool[_deterministic_int(account_id, salt) % len(pool)]


def _pick_n_from_pool(pool: list[Any], n: int, account_id: str, salt: str) -> list[Any]:
    indices: list[int] = []
    attempt = 0
    while len(indices) < n and attempt < n * 10:
        idx = _deterministic_int(account_id, f"{salt}_{attempt}") % len(pool)
        if idx not in indices:
            indices.append(idx)
        attempt += 1
    return [pool[i] for i in indices]


def _mobile_device_profile(account_id: str) -> dict[str, Any]:
    requested = os.getenv("BROWSER_MOBILE_DEVICE", "").strip().lower()
    if requested:
        normalized = re.sub(r"[^a-z0-9]+", "_", requested).strip("_")
        for profile in _MOBILE_DEVICE_PROFILES:
            candidates = {
                str(profile.get("id") or "").lower(),
                re.sub(r"[^a-z0-9]+", "_", str(profile.get("brand") or "").lower()).strip("_"),
                re.sub(r"[^a-z0-9]+", "_", str(profile.get("model") or "").lower()).strip("_"),
                f"{profile.get('brand', '')}_{profile.get('model', '')}".lower().replace(" ", "_"),
            }
            if normalized in candidates:
                return profile
    return _pick_from_pool(_MOBILE_DEVICE_PROFILES, account_id, "mobile_device_profile")


def _mobile_profile_defaults(account_id: str) -> dict[str, Any]:
    device = _mobile_device_profile(account_id)
    width, height, dpr = _pick_from_pool(list(device["viewports"]), account_id, f"{device['id']}_viewport")
    chrome_major = os.getenv("BROWSER_CHROME_MAJOR", "147").strip() or "147"
    android_major = str(device.get("android") or "13")
    model = str(device.get("model") or "Pixel 7")
    user_agent = (
        f"Mozilla/5.0 (Linux; Android {android_major}; {model}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{chrome_major}.0.0.0 Mobile Safari/537.36"
    )
    font_pool_index = int(device.get("fonts_index") or 0) % len(_ANDROID_FONT_POOLS)
    return {
        "device_id": device["id"],
        "brand": device["brand"],
        "model": model,
        "android": android_major,
        "screen_resolution": {"width": width, "height": height},
        "device_scale_factor": dpr,
        "user_agent": user_agent,
        "sec_ch_ua": _SEC_CH_UA_MAP.get(chrome_major, _DEFAULT_MOBILE_SEC_CH_UA),
        "hardware_concurrency": _pick_from_pool(list(device["hardware_concurrency"]), account_id, "mobile_cores"),
        "device_memory": _pick_from_pool(list(device["device_memory"]), account_id, "mobile_mem"),
        "fonts": list(_ANDROID_FONT_POOLS[font_pool_index]),
        "webgl_renderer": device["webgl_renderer"],
        "webgl_vendor": device["webgl_vendor"],
    }


def _permission_states_for_account(account_id: str, is_mobile: bool) -> dict[str, str]:
    states = dict(_PERMISSION_BASE_STATES)
    # Real profiles are sticky: some users have denied one or two sensitive
    # prompts, but broad "granted" access would be suspicious on first visit.
    for key in _PERMISSION_VARIANT_KEYS:
        roll = _deterministic_int(account_id, f"perm:{key}") % 10
        if roll == 0:
            states[key] = "denied"
        elif roll == 1 and key in {"notifications", "geolocation"}:
            states[key] = "granted" if not is_mobile else "prompt"
    return states


def _storage_estimate_for_account(account_id: str, is_mobile: bool) -> dict[str, Any]:
    base_quota = 128 * 1024 * 1024 * 1024 if is_mobile else 512 * 1024 * 1024 * 1024
    quota_jitter = (_deterministic_int(account_id, "storage_quota") % (24 * 1024)) * 1024 * 1024
    indexed_db = 4_000_000 + (_deterministic_int(account_id, "storage_indexeddb") % 18_000_000)
    caches = 1_000_000 + (_deterministic_int(account_id, "storage_caches") % 12_000_000)
    service_workers = _deterministic_int(account_id, "storage_sw") % 900_000
    usage = indexed_db + caches + service_workers
    return {
        "quota": base_quota - quota_jitter,
        "usage": usage,
        "usageDetails": {
            "indexedDB": indexed_db,
            "caches": caches,
            "serviceWorkerRegistrations": service_workers,
        },
    }


def _desktop_voices_for_account(account_id: str, locale: str) -> list[dict[str, Any]]:
    if locale.startswith("en-GB"):
        pool = [
            ("Microsoft Sonia Online (Natural) - English (United Kingdom)", "en-GB", True),
            ("Google UK English Female", "en-GB", False),
            ("Google UK English Male", "en-GB", False),
        ]
    else:
        pool = [
            ("Microsoft Aria Online (Natural) - English (United States)", "en-US", True),
            ("Microsoft Jenny Online (Natural) - English (United States)", "en-US", True),
            ("Google US English", "en-US", False),
            ("Google UK English Female", "en-GB", False),
        ]
    count = 2 + (_deterministic_int(account_id, "voice_count") % min(2, len(pool) - 1))
    selected = _pick_n_from_pool(pool, count, account_id, "voices")
    return [
        {
            "voiceURI": name,
            "name": name,
            "lang": lang,
            "localService": local,
            "default": index == 0,
        }
        for index, (name, lang, local) in enumerate(selected)
    ]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _chrome_major_from_ua(user_agent: str, fallback: str = "120") -> str:
    match = re.search(r"Chrome/(\d+)", user_agent)
    return match.group(1) if match else fallback


def _android_version_from_ua(user_agent: str, fallback: str = "13.0.0") -> str:
    match = re.search(r"Android\s+([0-9]+(?:\.[0-9]+)*)", user_agent)
    if not match:
        return fallback
    parts = match.group(1).split(".")
    while len(parts) < 3:
        parts.append("0")
    return ".".join(parts[:3])


def _apply_env_profile_overrides(profile: dict) -> dict:
    """Apply optional .env browser profile overrides.

    Set BROWSER_PROFILE_IS_ACTIVE=true or BROWSER_DEVICE_CATEGORY=mobile to
    activate the mobile profile fields.
    """
    timezone = os.getenv("BROWSER_TIMEZONE", "").strip()
    if timezone:
        profile["timezone"] = timezone
        profile["locale"] = os.getenv(
            "BROWSER_LOCALE",
            _TIMEZONE_LOCALE_MAP.get(timezone, profile.get("locale", "en-US")),
        ).strip()

    device_category = os.getenv("BROWSER_DEVICE_CATEGORY", "").strip().lower()
    profile_active = _env_bool("BROWSER_PROFILE_IS_ACTIVE", _env_bool("BROWSER_IS_ACTIVE", False))
    if not profile_active and device_category != "mobile":
        return profile

    account_id = str(profile.get("account_id") or "account_1")
    mobile_defaults = _mobile_profile_defaults(account_id)
    user_agent = os.getenv("BROWSER_USER_AGENT", str(mobile_defaults["user_agent"])).strip()
    chrome_major = _chrome_major_from_ua(user_agent)
    sec_ch_ua = os.getenv(
        "BROWSER_SEC_CH_UA",
        _SEC_CH_UA_MAP.get(chrome_major, str(mobile_defaults["sec_ch_ua"])),
    ).strip()
    sec_ch_ua_mobile = os.getenv("BROWSER_SEC_CH_UA_MOBILE", "?1").strip()
    sec_ch_ua_platform = os.getenv("BROWSER_SEC_CH_UA_PLATFORM", '"Android"').strip()
    platform = os.getenv("BROWSER_PLATFORM", "Linux armv8l").strip()

    default_screen = mobile_defaults["screen_resolution"]
    lock_screen = _env_bool("BROWSER_LOCK_SCREEN", False)
    width = _env_int("BROWSER_WIDTH", int(default_screen["width"])) if lock_screen else int(default_screen["width"])
    height = _env_int("BROWSER_HEIGHT", int(default_screen["height"])) if lock_screen else int(default_screen["height"])
    is_mobile = device_category == "mobile" or sec_ch_ua_mobile == "?1"
    storage_estimate = _storage_estimate_for_account(account_id, is_mobile)

    profile.update({
        "screen_resolution": {"width": width, "height": height},
        "platform": platform,
        "device_category": device_category or ("mobile" if is_mobile else "desktop"),
        "is_mobile": is_mobile,
        "has_touch": _env_bool("BROWSER_HAS_TOUCH", is_mobile),
        "max_touch_points": _env_int("BROWSER_MAX_TOUCH_POINTS", 5 if is_mobile else 0),
        "device_scale_factor": _env_float(
            "BROWSER_DEVICE_SCALE_FACTOR",
            float(mobile_defaults["device_scale_factor"]) if is_mobile else float(profile.get("device_scale_factor", 1)),
        ) if lock_screen else (float(mobile_defaults["device_scale_factor"]) if is_mobile else float(profile.get("device_scale_factor", 1))),
        "hardware_concurrency": _env_int(
            "BROWSER_HARDWARE_CONCURRENCY",
            int(mobile_defaults["hardware_concurrency"]) if is_mobile else int(profile.get("hardware_concurrency", 8)),
        ),
        "device_memory": _env_int(
            "BROWSER_DEVICE_MEMORY",
            int(mobile_defaults["device_memory"]) if is_mobile else int(profile.get("device_memory", 8)),
        ),
        "user_agent": user_agent,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_mobile": sec_ch_ua_mobile,
        "sec_ch_ua_platform": sec_ch_ua_platform,
        "chrome_full_version": os.getenv("BROWSER_CHROME_FULL_VERSION", "").strip(),
        "timezone": os.getenv("BROWSER_TIMEZONE", profile.get("timezone", "America/New_York")).strip(),
        "locale": os.getenv(
            "BROWSER_LOCALE",
            _TIMEZONE_LOCALE_MAP.get(profile.get("timezone", "America/New_York"), profile.get("locale", "en-US")),
        ).strip(),
        "mobile_device_id": mobile_defaults.get("device_id", ""),
        "mobile_brand": mobile_defaults.get("brand", ""),
        "mobile_model": os.getenv("BROWSER_MODEL", str(mobile_defaults.get("model") or "Pixel 7")).strip(),
        "mobile_platform_version": os.getenv(
            "BROWSER_PLATFORM_VERSION",
            _android_version_from_ua(user_agent),
        ).strip(),
        "architecture": os.getenv("BROWSER_ARCHITECTURE", "arm" if is_mobile else "x86").strip(),
        "bitness": os.getenv("BROWSER_BITNESS", "64").strip(),
        "webgl_renderer": os.getenv(
            "BROWSER_WEBGL_RENDERER",
            str(mobile_defaults.get("webgl_renderer") or "ANGLE (ARM, Mali-G710, OpenGL ES 3.2)"),
        ).strip(),
        "webgl_vendor": os.getenv(
            "BROWSER_WEBGL_VENDOR",
            str(mobile_defaults.get("webgl_vendor") or "Google Inc. (ARM)"),
        ).strip(),
        "plugins": [] if is_mobile else profile.get("plugins", []),
        "mime_types": [] if is_mobile else [
            {"type": "application/pdf", "suffixes": "pdf", "description": "Portable Document Format"},
            {"type": "text/pdf", "suffixes": "pdf", "description": "Portable Document Format"},
        ],
        "fonts": list(mobile_defaults["fonts"]) if is_mobile else profile.get("fonts", []),
        "permission_states": _permission_states_for_account(account_id, is_mobile),
        "storage_estimate": storage_estimate,
        "connection": {
            "effectiveType": os.getenv("BROWSER_EFFECTIVE_TYPE", "4g").strip(),
            "rtt": _env_int("BROWSER_RTT", 80 if is_mobile else 50),
            "downlink": _env_float("BROWSER_DOWNLINK", 12.0 if is_mobile else 10.0),
            "saveData": _env_bool("BROWSER_SAVE_DATA", False),
            "type": os.getenv("BROWSER_CONNECTION_TYPE", "cellular" if is_mobile else "wifi").strip(),
        },
    })
    return profile


@dataclass
class BrowserProfileManager:
    """Generate, inject, and compare deterministic browser fingerprint profiles."""

    def generate(self, account_id: str, timezone: str = "America/New_York") -> dict:
        """Return a deterministic browser profile dict for *account_id*."""
        timezone = os.getenv("BROWSER_TIMEZONE", timezone).strip() or "America/New_York"
        canvas_noise_seed = _deterministic_int(account_id, "canvas") % (2**32)
        webgl_renderer = _pick_from_pool(_WEBGL_RENDERERS, account_id, "webgl")
        width, height = _pick_from_pool(_SCREEN_RESOLUTIONS, account_id, "screen")
        hardware_concurrency = _pick_from_pool([4, 8], account_id, "cores")
        device_memory = _pick_from_pool([8, 16], account_id, "mem")
        device_scale_factor = _pick_from_pool([1, 1, 1, 2], account_id, "dpr")

        plugin_count = 2 + (_deterministic_int(account_id, "plugcount") % 3)
        plugins = _pick_n_from_pool(_PLUGIN_POOL, plugin_count, account_id, "plugins")

        optional_font_count = 2 + (_deterministic_int(account_id, "fontcount") % 2)
        optional_fonts = _pick_n_from_pool(_OPTIONAL_FONTS, optional_font_count, account_id, "fonts")
        fonts = _BASE_FONTS + optional_fonts

        locale = _TIMEZONE_LOCALE_MAP.get(timezone, "en-US")

        user_agent = _pick_from_pool(_USER_AGENT_TEMPLATES, account_id, "ua")
        _chrome_major = _chrome_major_from_ua(user_agent, fallback="131")
        sec_ch_ua = _SEC_CH_UA_MAP.get(_chrome_major, _DEFAULT_SEC_CH_UA)

        profile = {
            "account_id": account_id,
            "canvas_noise_seed": canvas_noise_seed,
            "webgl_renderer": webgl_renderer,
            "webgl_vendor": "Google Inc. (Intel)" if "Intel" in webgl_renderer
                else "Google Inc. (NVIDIA)" if "NVIDIA" in webgl_renderer
                else "Google Inc. (AMD)",
            "screen_resolution": {"width": width, "height": height},
            "timezone": timezone,
            "locale": locale,
            "plugins": plugins,
            "mime_types": [
                {"type": "application/pdf", "suffixes": "pdf", "description": "Portable Document Format"},
                {"type": "text/pdf", "suffixes": "pdf", "description": "Portable Document Format"},
            ],
            "platform": "Win32",
            "fonts": fonts,
            "hardware_concurrency": hardware_concurrency,
            "device_memory": device_memory,
            "device_scale_factor": device_scale_factor,
            "user_agent": user_agent,
            "sec_ch_ua": sec_ch_ua,
            "sec_ch_ua_platform": '"Windows"',
            "sec_ch_ua_mobile": "?0",
            "chrome_full_version": "",
            "device_category": "desktop",
            "is_mobile": False,
            "has_touch": False,
            "max_touch_points": 0,
            "permission_states": _permission_states_for_account(account_id, False),
            "storage_estimate": _storage_estimate_for_account(account_id, False),
            "voices": _desktop_voices_for_account(account_id, locale),
        }
        return _apply_env_profile_overrides(profile)

    async def inject(self, page: Page, profile: dict) -> None:
        """Inject fingerprint overrides into *page* via addInitScript.
        Must be called before any page.goto().
        """
        script = _build_inject_script(profile)
        await page.add_init_script(script)

    def check_collision(self, new_profile: dict, existing: list[dict]) -> bool:
        """Return True if new_profile is too similar to any in existing (>90% match)."""
        for other in existing:
            matches = sum(
                1 for key in _COLLISION_KEYS
                if new_profile.get(key) == other.get(key)
            )
            if len(_COLLISION_KEYS) > 0 and (matches / len(_COLLISION_KEYS)) > 0.9:
                return True
        return False


def _build_inject_script(profile: dict) -> str:
    """Build the JS string that overrides browser fingerprint properties."""
    screen = profile["screen_resolution"]
    width = screen["width"]
    height = screen["height"]
    platform = profile["platform"]
    locale = profile["locale"]
    hw_concurrency = profile["hardware_concurrency"]
    dev_memory = profile["device_memory"]
    webgl_vendor = profile["webgl_vendor"]
    webgl_renderer = profile["webgl_renderer"]
    canvas_seed = profile["canvas_noise_seed"]
    plugins_json = json.dumps(profile["plugins"])
    mime_types_json = json.dumps(profile.get("mime_types") or [])
    pdf_viewer_enabled = bool(profile.get("plugins"))
    timezone = profile["timezone"]
    max_touch_points = int(profile.get("max_touch_points", 0))
    is_mobile = bool(profile.get("is_mobile", False))
    outer_width_delta = 0 if is_mobile else 15
    outer_height_delta = 0 if is_mobile else 85
    connection = profile.get("connection") or {
        "effectiveType": "4g",
        "rtt": 50,
        "downlink": 10,
        "saveData": False,
        "type": "wifi",
    }
    connection_json = json.dumps(connection)

    return f"""
(() => {{
    // --- Native-looking Function.prototype.toString chain ----------------------
    const __origFunctionToString = Function.prototype.toString;
    const __nativeFunctionSources = new WeakMap();
    const __nativeSource = (name) => 'function ' + (name || '') + '() {{ [native code] }}';
    const __markNative = (fn, name) => {{
        try {{
            if (typeof fn === 'function') {{
                __nativeFunctionSources.set(fn, __nativeSource(name || fn.name || ''));
            }}
        }} catch(_) {{}}
        return fn;
    }};
    const __functionToString = function toString() {{
        try {{
            if (__nativeFunctionSources.has(this)) return __nativeFunctionSources.get(this);
        }} catch(_) {{}}
        return __origFunctionToString.call(this);
    }};
    __markNative(__origFunctionToString, 'toString');
    __markNative(__functionToString, 'toString');
    try {{
        Object.defineProperty(Function.prototype, 'toString', {{
            value: __functionToString,
            configurable: true,
            writable: true,
        }});
    }} catch(_) {{}}

    // --- Helper to define getters on prototypes with proper native check & illegal invocation throw ---
    const _defineSafeGetter = (proto, prop, valOrFn, expectedInstance) => {{
        const getVal = typeof valOrFn === 'function' ? valOrFn : () => valOrFn;
        const safeGetter = function() {{
            if (expectedInstance) {{
                if (this !== expectedInstance) {{
                    throw new TypeError("Illegal invocation");
                }}
            }} else {{
                if (!(this instanceof proto.constructor) && this !== proto) {{
                    throw new TypeError("Illegal invocation");
                }}
            }}
            return getVal.call(this);
        }};
        Object.defineProperty(safeGetter, 'name', {{ value: `get ${{prop}}`, configurable: true }});
        __markNative(safeGetter, `get ${{prop}}`);
        Object.defineProperty(proto, prop, {{
            get: safeGetter,
            configurable: true,
            enumerable: true
        }});
    }};

    // --- Navigator overrides ---------------------------------------------------
    _defineSafeGetter(Navigator.prototype, 'platform', {json.dumps(platform)}, navigator);
    _defineSafeGetter(Navigator.prototype, 'language', {json.dumps(locale)}, navigator);
    _defineSafeGetter(Navigator.prototype, 'languages', () => [{json.dumps(locale)}, 'en'], navigator);
    _defineSafeGetter(Navigator.prototype, 'hardwareConcurrency', {hw_concurrency}, navigator);
    _defineSafeGetter(Navigator.prototype, 'deviceMemory', {dev_memory}, navigator);

    // --- Screen overrides ------------------------------------------------------
    _defineSafeGetter(Screen.prototype, 'width', {width}, screen);
    _defineSafeGetter(Screen.prototype, 'height', {height}, screen);
    _defineSafeGetter(Screen.prototype, 'availWidth', {width}, screen);
    _defineSafeGetter(Screen.prototype, 'availHeight', {height - 40}, screen);
    _defineSafeGetter(Screen.prototype, 'colorDepth', 24, screen);
    _defineSafeGetter(Screen.prototype, 'pixelDepth', 24, screen);

    // --- Plugins / MimeTypes override -----------------------------------------
    const pluginData = {plugins_json};
    const mimeTypeData = {mime_types_json};
    const makeNativeLikeArray = (proto, values, namedKey) => {{
        const arr = typeof proto === 'function' ? Object.create(proto.prototype) : {{}};
        Object.defineProperty(arr, 'length', {{ get: () => values.length, configurable: true }});
        Object.defineProperty(arr, 'item', {{ value: (i) => values[i] || null, configurable: true }});
        Object.defineProperty(arr, 'namedItem', {{
            value: (name) => values.find(v => v && v[namedKey] === name) || null,
            configurable: true,
        }});
        Object.defineProperty(arr, Symbol.iterator, {{
            value: function* () {{ for (const v of values) yield v; }},
            configurable: true,
        }});
        values.forEach((value, index) => {{
            Object.defineProperty(arr, index, {{ value, enumerable: true, configurable: true }});
            if (value && value[namedKey]) {{
                try {{ Object.defineProperty(arr, value[namedKey], {{ value, configurable: true }}); }} catch(_) {{}}
            }}
        }});
        return arr;
    }};
    const fakePlugins = makeNativeLikeArray(window.PluginArray, pluginData, 'name');
    const pluginRefresh = __markNative(function refresh() {{}}, 'refresh');
    Object.defineProperty(fakePlugins, 'refresh', {{ value: pluginRefresh, configurable: true }});
    const fakeMimeTypes = makeNativeLikeArray(window.MimeTypeArray, mimeTypeData, 'type');
    _defineSafeGetter(Navigator.prototype, 'plugins', () => fakePlugins, navigator);
    _defineSafeGetter(Navigator.prototype, 'mimeTypes', () => fakeMimeTypes, navigator);
    _defineSafeGetter(Navigator.prototype, 'pdfViewerEnabled', {json.dumps(pdf_viewer_enabled)}, navigator);

    // --- WebGL overrides -------------------------------------------------------
    const getParamOrig = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
        if (!(this instanceof WebGLRenderingContext)) {{
            throw new TypeError("Failed to execute 'getParameter' on 'WebGLRenderingContext': Illegal invocation");
        }}
        const UNMASKED_VENDOR = 0x9245;
        const UNMASKED_RENDERER = 0x9246;
        if (param === UNMASKED_VENDOR) return {json.dumps(webgl_vendor)};
        if (param === UNMASKED_RENDERER) return {json.dumps(webgl_renderer)};
        return getParamOrig.call(this, param);
    }};
    __markNative(WebGLRenderingContext.prototype.getParameter, 'getParameter');
    if (typeof WebGL2RenderingContext !== 'undefined') {{
        const getParam2Orig = WebGL2RenderingContext.prototype.getParameter;
        WebGL2RenderingContext.prototype.getParameter = function(param) {{
            if (!(this instanceof WebGL2RenderingContext)) {{
                throw new TypeError("Failed to execute 'getParameter' on 'WebGL2RenderingContext': Illegal invocation");
            }}
            const UNMASKED_VENDOR = 0x9245;
            const UNMASKED_RENDERER = 0x9246;
            if (param === UNMASKED_VENDOR) return {json.dumps(webgl_vendor)};
            if (param === UNMASKED_RENDERER) return {json.dumps(webgl_renderer)};
            return getParam2Orig.call(this, param);
        }};
        __markNative(WebGL2RenderingContext.prototype.getParameter, 'getParameter');
    }}

    // --- navigator.webdriver (critical bot signal) ------------------------------
    try {{ delete navigator.webdriver; }} catch(_) {{}}
    try {{
        const webdriverGetter = __markNative(function webdriver() {{
            if (this !== navigator) {{
                throw new TypeError("Failed to execute 'webdriver' on 'Navigator': Illegal invocation");
            }}
            return false;
        }}, 'get webdriver');
        Object.defineProperty(Navigator.prototype, 'webdriver', {{
            get: webdriverGetter,
            enumerable: true,
            configurable: true,
        }});
    }} catch(_) {{}}

    // --- window.chrome stub ----------------------------------------------------
    if (!window.chrome) {{
        window.chrome = {{
            app: {{
                isInstalled: false,
                getDetails: __markNative(function getDetails() {{ return null; }}, 'getDetails'),
                getIsInstalled: __markNative(function getIsInstalled() {{ return false; }}, 'getIsInstalled'),
            }},
            runtime: {{
                connect: __markNative(function connect() {{ return undefined; }}, 'connect'),
                sendMessage: __markNative(function sendMessage() {{ return undefined; }}, 'sendMessage'),
            }},
            csi: __markNative(function csi() {{ return {{}}; }}, 'csi'),
            loadTimes: __markNative(function loadTimes() {{
                return {{
                    commitLoadTime: Date.now() / 1000 - 1.2,
                    connectionInfo: 'h2',
                    finishDocumentLoadTime: Date.now() / 1000 - 0.3,
                    finishLoadTime: Date.now() / 1000 - 0.1,
                    firstPaintAfterLoadTime: 0,
                    firstPaintTime: Date.now() / 1000 - 0.8,
                    navigationType: 'Other',
                    npnNegotiatedProtocol: 'h2',
                    requestTime: Date.now() / 1000 - 1.5,
                    startLoadTime: Date.now() / 1000 - 1.5,
                    wasAlternateProtocolAvailable: false,
                    wasFetchedViaSpdy: true,
                    wasNpnNegotiated: true,
                }};
            }}, 'loadTimes'),
        }};
    }}

    // --- Canvas noise (per-account pixel noise defeats canvas fingerprinting) ---
    const canvasSeed = {canvas_seed};
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const __withTinyCanvasNoise = (canvas, fn) => {{
        const ctx = canvas.getContext('2d');
        if (!ctx || canvas.width <= 0 || canvas.height <= 0) return fn();
        let imageData = null;
        const changed = [];
        try {{
            imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
            const data = imageData.data;
            const stride = Math.max(4096, Math.floor(data.length / 12));
            let s = canvasSeed ^ (canvas.width << 8) ^ canvas.height;
            for (let i = ((s >>> 3) % 64) * 4; i < data.length; i += stride) {{
                s = (s * 1103515245 + 12345) & 0x7fffffff;
                changed.push([i, data[i]]);
                data[i] = (data[i] + (s % 3 - 1)) & 0xff;
            }}
            if (changed.length) ctx.putImageData(imageData, 0, 0);
        }} catch(_) {{
            imageData = null;
        }}
        try {{
            return fn();
        }} finally {{
            if (imageData && changed.length) {{
                try {{
                    for (const item of changed) imageData.data[item[0]] = item[1];
                    ctx.putImageData(imageData, 0, 0);
                }} catch(_) {{}}
            }}
        }}
    }};
    HTMLCanvasElement.prototype.toDataURL = function(type, quality) {{
        if (!(this instanceof HTMLCanvasElement)) {{
            throw new TypeError("Failed to execute 'toDataURL' on 'HTMLCanvasElement': Illegal invocation");
        }}
        return __withTinyCanvasNoise(this, () => origToDataURL.call(this, type, quality));
    }};
    __markNative(HTMLCanvasElement.prototype.toDataURL, 'toDataURL');

    // --- AudioContext fingerprint noise (defeats Reddit/DataDome audio hash) ----
    const audioSeed = canvasSeed ^ 0xDEADBEEF;
    const origGetChannelData = AudioBuffer.prototype.getChannelData;
    AudioBuffer.prototype.getChannelData = function(channel) {{
        if (!(this instanceof AudioBuffer)) {{
            throw new TypeError("Failed to execute 'getChannelData' on 'AudioBuffer': Illegal invocation");
        }}
        const data = origGetChannelData.call(this, channel);
        if (this.__noised) return data;
        this.__noised = true;
        let s = audioSeed;
        for (let i = 0; i < data.length; i += 100) {{
            s = (s * 1103515245 + 12345) & 0x7fffffff;
            data[i] += (s % 3 - 1) * 0.0000001;
        }}
        return data;
    }};
    __markNative(AudioBuffer.prototype.getChannelData, 'getChannelData');

    // --- Timezone override (Intl) -----------------------------------------------
    const tz = {json.dumps(timezone)};
    const OrigDateTimeFormat = Intl.DateTimeFormat;
    const newDTF = function(locales, options) {{
        options = Object.assign({{}}, options || {{}});
        options.timeZone = options.timeZone || tz;
        return new OrigDateTimeFormat(locales, options);
    }};
    newDTF.prototype = OrigDateTimeFormat.prototype;
    newDTF.supportedLocalesOf = OrigDateTimeFormat.supportedLocalesOf;
    Object.defineProperty(Intl, 'DateTimeFormat', {{ value: newDTF, writable: true, configurable: true }});
    __markNative(Intl.DateTimeFormat, 'DateTimeFormat');

    // --- Other navigator properties --------------------------------------------
    _defineSafeGetter(Navigator.prototype, 'maxTouchPoints', {max_touch_points}, navigator);
    _defineSafeGetter(Navigator.prototype, 'doNotTrack', null, navigator);

    // --- performance.memory (Chrome fingerprint check) ------------------------
    if (window.performance) {{
        Object.defineProperty(performance, 'memory', {{
            get: () => ({{
                jsHeapSizeLimit: 2172649472,
                totalJSHeapSize: 35839897 + (canvasSeed % 5000000),
                usedJSHeapSize: 28723145 + (canvasSeed % 3000000),
            }}),
        }});
    }}

    // --- Battery API stub (deprecated but fingerprinted) ----------------------
    if (navigator.getBattery) {{
        navigator.getBattery = __markNative(function getBattery() {{
            if (this !== navigator) {{
                throw new TypeError("Failed to execute 'getBattery' on 'Navigator': Illegal invocation");
            }}
            return Promise.resolve({{
                charging: true, chargingTime: 0, dischargingTime: Infinity, level: 1.0,
                addEventListener: function() {{}}, removeEventListener: function() {{}},
                dispatchEvent: function() {{ return true; }},
                onchargingchange: null, onchargingtimechange: null,
                ondischargingtimechange: null, onlevelchange: null,
            }});
        }}, 'getBattery');
    }}

    // --- Network Connection API ------------------------------------------------
    const connectionData = {connection_json};
    if (!navigator.connection) {{
        _defineSafeGetter(Navigator.prototype, 'connection', () => Object.assign({{
            addEventListener: function() {{}},
            removeEventListener: function() {{}},
        }}, connectionData), navigator);
    }}

    // --- mediaDevices.enumerateDevices() fake ---------------------------------
    if (navigator.mediaDevices && navigator.mediaDevices.enumerateDevices) {{
        navigator.mediaDevices.enumerateDevices = function() {{
            return Promise.resolve([
                {{ deviceId: 'default', kind: 'audioinput', label: '', groupId: 'default' }},
                {{ deviceId: 'default', kind: 'audiooutput', label: '', groupId: 'default' }},
                {{ deviceId: 'default', kind: 'videoinput', label: '', groupId: 'default' }},
            ]);
        }};
        __markNative(navigator.mediaDevices.enumerateDevices, 'enumerateDevices');
    }}

    // --- WebRTC ICE candidate filtering ---------------------------------------
    const OrigRTCPeerConnection = window.RTCPeerConnection || window.webkitRTCPeerConnection;
    if (OrigRTCPeerConnection) {{
        const _IS_RAW_IP = /(\\d+\\.\\d+\\.\\d+\\.\\d+|[0-9a-fA-F:]+:[0-9a-fA-F:]+)/;
        const _IS_MDNS   = /\\s[\\w-]+\\.local(?:\\s|$)/i;
        const _candidateLeaksRealIP = function(c) {{
            if (!c) return false;
            if (/typ\\s+(srflx\\s+|prflx\\s+)/i.test(c)) return true;
            if (/typ\\s+host/i.test(c)) {{
                if (_IS_MDNS.test(c)) return false;
                if (_IS_RAW_IP.test(c)) return true;
            }}
            return false;
        }};
        const _wrapIceListener = function(listener) {{
            return function(event) {{
                try {{
                    if (event && event.candidate && event.candidate.candidate) {{
                        if (_candidateLeaksRealIP(event.candidate.candidate)) return;
                    }}
                }} catch (_) {{}}
                return listener.apply(this, arguments);
            }};
        }};
        const _origOnIceDesc = Object.getOwnPropertyDescriptor(
            OrigRTCPeerConnection.prototype, 'onicecandidate'
        );
        const wrappedRTC = function(config, constraints) {{
            const pc = new OrigRTCPeerConnection(config, constraints);
            const origAddEventListener = pc.addEventListener.bind(pc);
            pc.addEventListener = function(type, listener, options) {{
                if (type === 'icecandidate' && typeof listener === 'function') {{
                    return origAddEventListener(type, _wrapIceListener(listener), options);
                }}
                return origAddEventListener(type, listener, options);
            }};
            if (_origOnIceDesc && _origOnIceDesc.set) {{
                let _userFn = null;
                try {{
                    Object.defineProperty(pc, 'onicecandidate', {{
                        get: function() {{ return _userFn; }},
                        set: function(fn) {{
                            _userFn = (typeof fn === 'function') ? fn : null;
                            const wrapped = (typeof fn === 'function')
                                ? _wrapIceListener(fn) : fn;
                            _origOnIceDesc.set.call(pc, wrapped);
                        }},
                        configurable: true,
                        enumerable: true,
                    }});
                }} catch (_) {{}}
            }}
            return pc;
        }};
        wrappedRTC.prototype = OrigRTCPeerConnection.prototype;
        wrappedRTC.generateCertificate = OrigRTCPeerConnection.generateCertificate;
        __markNative(wrappedRTC, 'RTCPeerConnection');
        window.RTCPeerConnection = wrappedRTC;
        if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = wrappedRTC;
    }}

    // --- Error.stack cleanup (remove Playwright evaluation traces) -------------
    if (typeof Error.prepareStackTrace === 'undefined' || Error.prepareStackTrace === null) {{
        Error.prepareStackTrace = function(err, structuredStackTrace) {{
            const filtered = structuredStackTrace.filter(function(frame) {{
                const fn = (frame.getFunctionName && frame.getFunctionName()) || '';
                const file = (frame.getFileName && frame.getFileName()) || '';
                return fn.indexOf('__playwright') === -1 && fn.indexOf('__puppeteer') === -1 &&
                       file.indexOf('__playwright') === -1 && file.indexOf('pptr:') === -1;
            }});
            return 'Error: ' + (err.message || '') + '\\n' +
                   filtered.map(function(f) {{
                       return '    at ' + (f.getFunctionName() || '<anonymous>') +
                              ' (' + (f.getFileName() || '<anonymous>') + ':' +
                              (f.getLineNumber() || 0) + ':' + (f.getColumnNumber() || 0) + ')';
                   }}).join('\\n');
        }};
    }}

    // --- window.outerWidth/outerHeight consistency ----------------------------
    Object.defineProperty(window, 'outerWidth', {{
        get: __markNative(function outerWidth() {{
            if (this !== window) throw new TypeError("Illegal invocation");
            return window.innerWidth + {outer_width_delta};
        }}, 'get outerWidth'),
        configurable: true
    }});
    Object.defineProperty(window, 'outerHeight', {{
        get: __markNative(function outerHeight() {{
            if (this !== window) throw new TypeError("Illegal invocation");
            return window.innerHeight + {outer_height_delta};
        }}, 'get outerHeight'),
        configurable: true
    }});

    const _makeNonEnumerable = (proto, prop) => {{
        try {{
            if (!proto) return;
            const desc = Object.getOwnPropertyDescriptor(proto, prop);
            if (desc && desc.enumerable) {{
                desc.enumerable = false;
                Object.defineProperty(proto, prop, desc);
            }}
        }} catch(_) {{}}
    }};
    _makeNonEnumerable(WebGLRenderingContext.prototype, 'getParameter');
    if (typeof WebGL2RenderingContext !== 'undefined') {{
        _makeNonEnumerable(WebGL2RenderingContext.prototype, 'getParameter');
    }}
    _makeNonEnumerable(HTMLCanvasElement.prototype, 'toDataURL');
    _makeNonEnumerable(AudioBuffer.prototype, 'getChannelData');
}})();
"""
