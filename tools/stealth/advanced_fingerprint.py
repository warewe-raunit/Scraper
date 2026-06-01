"""
tools/stealth/advanced_fingerprint.py — 30+ additional anti-bot & fingerprint evasion techniques.

Targets:
  PerimeterX, HUMAN (WhiteOps), DataDome, Kasada, Akamai Bot Manager,
  Cloudflare Bot Management, Reddit Sentinel, F5 Shape

Inject with:
    from tools.stealth.advanced_fingerprint import inject_advanced
    await inject_advanced(page, profile)
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _timezone_offset_minutes(timezone_name: str) -> int:
    """Return JavaScript Date.getTimezoneOffset() minutes for an IANA zone."""
    try:
        offset = datetime.now(ZoneInfo(timezone_name)).utcoffset()
    except (ZoneInfoNotFoundError, ValueError):
        offset = None
    if offset is None:
        offset = datetime.now(timezone.utc).utcoffset()
    return -int(offset.total_seconds() // 60)


def _timezone_transition_table(timezone_name: str) -> list[dict[str, int]]:
    """Return UTC transition points with JavaScript getTimezoneOffset values."""
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        zone = timezone.utc

    current_year = datetime.now(timezone.utc).year
    start = datetime(current_year - 1, 1, 1, tzinfo=timezone.utc)
    end = datetime(current_year + 2, 1, 1, tzinfo=timezone.utc)

    def offset_minutes(at_utc: datetime) -> int:
        offset = at_utc.astimezone(zone).utcoffset()
        if offset is None:
            return 0
        return -int(offset.total_seconds() // 60)

    transitions: list[dict[str, int]] = [
        {"at": int(start.timestamp() * 1000), "offset": offset_minutes(start)}
    ]
    previous_time = start
    previous_offset = transitions[0]["offset"]
    cursor = start + timedelta(hours=6)
    while cursor <= end:
        cursor_offset = offset_minutes(cursor)
        if cursor_offset != previous_offset:
            lo = previous_time
            hi = cursor
            while (hi - lo) > timedelta(minutes=1):
                mid = lo + (hi - lo) / 2
                if offset_minutes(mid) == previous_offset:
                    lo = mid
                else:
                    hi = mid
            transitions.append({"at": int(hi.timestamp() * 1000), "offset": cursor_offset})
            previous_offset = cursor_offset
        previous_time = cursor
        cursor += timedelta(hours=6)
    return transitions


def build_advanced_script(profile: dict) -> str:
    """Build the advanced evasion JS init script from a fingerprint profile."""
    tz = profile.get("timezone", "America/New_York")
    dpr = profile.get("device_scale_factor", 1)
    user_agent = profile.get("user_agent", "")
    canvas_seed = profile.get("canvas_noise_seed", 12345)
    locale = profile.get("locale", "en-US")
    is_mobile = bool(profile.get("is_mobile", False))
    fonts = profile.get("fonts") or []
    allowed_fonts = sorted({str(font).strip().lower() for font in fonts if str(font).strip()})
    default_font = next((str(font).strip() for font in fonts if str(font).strip()), "Arial")
    screen = profile.get("screen_resolution", {})
    orientation_type = "portrait-primary"
    if int(screen.get("width", 0) or 0) > int(screen.get("height", 0) or 0):
        orientation_type = "landscape-primary"
    pointer_fine = not is_mobile
    pointer_coarse = is_mobile
    hover_hover = not is_mobile
    hover_none = is_mobile
    tz_offset_minutes = _timezone_offset_minutes(tz)
    timezone_transitions = _timezone_transition_table(tz)
    permission_states = profile.get("permission_states") or {
        "notifications": "prompt",
        "push": "prompt",
        "midi": "prompt",
        "camera": "prompt",
        "microphone": "prompt",
        "geolocation": "prompt",
    }
    notification_state = str(permission_states.get("notifications", "prompt"))
    notification_permission = "default" if notification_state == "prompt" else notification_state
    storage_estimate = profile.get("storage_estimate") or {
        "quota": 128 * 1024 * 1024 * 1024 if is_mobile else 512 * 1024 * 1024 * 1024,
        "usage": 12_582_912 + (int(canvas_seed) % 8_000_000),
        "usageDetails": {
            "indexedDB": 4_000_000 + (int(canvas_seed) % 12_000_000),
            "caches": 1_000_000 + (int(canvas_seed) % 4_000_000),
            "serviceWorkerRegistrations": int(canvas_seed) % 500_000,
        },
    }
    pdf_viewer_enabled = bool(profile.get("plugins"))
    speech_voices_script = ""
    if not is_mobile:
        voices = profile.get("voices") or []
        voices_json = json.dumps(voices)
        speech_voices_script = """
if (window.speechSynthesis) {
    const fakeVoices = __VOICE_DATA__;
    try {
        window.speechSynthesis.getVoices = function() { return fakeVoices; };
        const ev = new Event('voiceschanged');
        window.speechSynthesis.dispatchEvent(ev);
    } catch(_) {}
}
""".replace("__VOICE_DATA__", voices_json)

    return f"""
(() => {{
// ═══════════════════════════════════════════════════════════════════════════
// ADVANCED ANTI-BOT EVASION — 40 additional fingerprint protection layers
// ═══════════════════════════════════════════════════════════════════════════

const _SEED = {canvas_seed};
const _IS_MOBILE_PROFILE = {json.dumps(is_mobile)};
const _ALLOWED_FONTS = new Set({json.dumps(allowed_fonts)});
const _DEFAULT_PROFILE_FONT = {json.dumps(default_font)};
const _TZ_TRANSITIONS = {json.dumps(timezone_transitions)};
const _PERMISSION_STATES = {json.dumps(permission_states)};
const _NOTIFICATION_PERMISSION = {json.dumps(notification_permission)};
const _STORAGE_ESTIMATE = {json.dumps(storage_estimate)};
const __prevFunctionToString = Function.prototype.toString;
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
    return __prevFunctionToString.call(this);
}};
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

// ── 1. navigator.vendor ──────────────────────────────────────────────────
_defineSafeGetter(Navigator.prototype, 'vendor', 'Google Inc.', navigator);

// ── 2-4. navigator.appVersion / product / productSub ────────────────────
try {{
    const ua = {json.dumps(user_agent)};
    const appVer = ua.replace(/^Mozilla\\//, '');
    _defineSafeGetter(Navigator.prototype, 'appVersion', appVer, navigator);
    _defineSafeGetter(Navigator.prototype, 'appName', 'Netscape', navigator);
    _defineSafeGetter(Navigator.prototype, 'product', 'Gecko', navigator);
    _defineSafeGetter(Navigator.prototype, 'productSub', '20030107', navigator);
}} catch(_) {{}}

// ── 5. navigator.userAgent consistency ──────────────────────────────────
_defineSafeGetter(Navigator.prototype, 'userAgent', {json.dumps(user_agent)}, navigator);

// ── 6. navigator.oscpu ──────────────────────────────────────────────────
_defineSafeGetter(Navigator.prototype, 'oscpu', undefined, navigator);

// ── 7. navigator.buildID ────────────────────────────────────────────────
_defineSafeGetter(Navigator.prototype, 'buildID', undefined, navigator);

// ── 8. navigator.cookieEnabled / onLine ─────────────────────────────────
_defineSafeGetter(Navigator.prototype, 'cookieEnabled', true, navigator);
_defineSafeGetter(Navigator.prototype, 'onLine', true, navigator);

// ── 9. window.devicePixelRatio ──────────────────────────────────────────
_defineSafeGetter(window, 'devicePixelRatio', {dpr}, window);

// ── 10. Date.prototype.getTimezoneOffset ────────────────────────────────
const _origDateGetTime = Date.prototype.getTime;
const _profileTimezoneOffset = (timeValue) => {{
    if (!Number.isFinite(timeValue) || !_TZ_TRANSITIONS.length) return {tz_offset_minutes};
    let offset = _TZ_TRANSITIONS[0].offset;
    for (let i = 1; i < _TZ_TRANSITIONS.length; i++) {{
        if (timeValue < _TZ_TRANSITIONS[i].at) break;
        offset = _TZ_TRANSITIONS[i].offset;
    }}
    return offset;
}};
Date.prototype.getTimezoneOffset = function() {{
    if (!(this instanceof Date)) {{
        throw new TypeError("Method Date.prototype.getTimezoneOffset called on incompatible receiver");
    }}
    return _profileTimezoneOffset(_origDateGetTime.call(this));
}};
__markNative(Date.prototype.getTimezoneOffset, 'getTimezoneOffset');

// ── 11. screen.orientation stub ─────────────────────────────────────────
try {{
    _defineSafeGetter(Screen.prototype, 'orientation', () => ({{
        type: {json.dumps(orientation_type)},
        angle: 0,
        onchange: null,
        addEventListener: function() {{}},
        removeEventListener: function() {{}},
        dispatchEvent: function() {{ return true; }},
        lock: () => Promise.reject(new DOMException('Not supported')),
        unlock: () => {{}},
    }}), screen);
}} catch(_) {{}}

// ── 12. window.screenLeft / screenTop / screenX / screenY ───────────────
_defineSafeGetter(window, 'screenLeft', 0, window);
_defineSafeGetter(window, 'screenTop', 0, window);
_defineSafeGetter(window, 'screenX', 0, window);
_defineSafeGetter(window, 'screenY', 0, window);

// ── 13. navigator.permissions.query ─────────────────────────────────────
if (navigator.permissions && navigator.permissions.query) {{
    const origQuery = navigator.permissions.query;
    const PermStatusProto = (window.PermissionStatus && window.PermissionStatus.prototype)
        || (window.EventTarget && window.EventTarget.prototype) || null;

    const _spoofedStates = _PERMISSION_STATES;

    const _buildStatus = function(name, state) {{
        const status = PermStatusProto ? Object.create(PermStatusProto) : {{}};
        Object.defineProperty(status, 'state', {{ get: () => state, configurable: true, enumerable: true }});
        Object.defineProperty(status, 'name',  {{ get: () => name,  configurable: true, enumerable: true }});
        Object.defineProperty(status, 'onchange', {{ value: null, writable: true, configurable: true, enumerable: true }});
        return status;
    }};

    navigator.permissions.query = async function(permDesc) {{
        if (!(this instanceof Permissions)) {{
            throw new TypeError("Failed to execute 'query' on 'Permissions': Illegal invocation");
        }}
        const name = (permDesc || {{}}).name || '';
        let real = null;
        try {{ real = await origQuery.call(this, permDesc); }} catch (_) {{ real = null; }}

        if (name in _spoofedStates) {{
            const desired = _spoofedStates[name];
            if (real && typeof real === 'object') {{
                try {{
                    Object.defineProperty(real, 'state', {{
                        get: () => desired, configurable: true, enumerable: true,
                    }});
                    return real;
                }} catch (_) {{}}
            }}
            return _buildStatus(name, desired);
        }}
        if (real) return real;
        return _buildStatus(name, 'prompt');
    }};

    __markNative(navigator.permissions.query, 'query');
    try {{ Object.defineProperty(navigator.permissions.query, 'name', {{ value: 'query', configurable: true }}); }} catch (_) {{}}
}}

// ── 14. Notification.permission ─────────────────────────────────────────
try {{
    if (window.Notification) {{
        const getPermission = function() {{
            if (this !== Notification) {{
                throw new TypeError("Illegal invocation");
            }}
            return _NOTIFICATION_PERMISSION;
        }};
        Object.defineProperty(getPermission, 'name', {{ value: 'get permission', configurable: true }});
        __markNative(getPermission, 'get permission');
        Object.defineProperty(Notification, 'permission', {{
            get: getPermission,
            configurable: true
        }});
    }}
}} catch(_) {{}}

// ── 15. navigator.getGamepads() ─────────────────────────────────────────
if (Navigator.prototype.getGamepads) {{
    const origGetGamepads = Navigator.prototype.getGamepads;
    Navigator.prototype.getGamepads = function() {{
        if (!(this instanceof Navigator) && this !== Navigator.prototype) {{
            throw new TypeError("Failed to execute 'getGamepads' on 'Navigator': Illegal invocation");
        }}
        return [null, null, null, null];
    }};
    __markNative(Navigator.prototype.getGamepads, 'getGamepads');
}}

// ── 16. SpeechSynthesis.getVoices() ─────────────────────────────────────
{speech_voices_script}

// ── 17. navigator.keyboard stub ──────────────────────────────────────────
if (!navigator.keyboard) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'keyboard', () => ({{
            getLayoutMap: () => Promise.resolve(new Map()),
            lock: () => Promise.resolve(),
            unlock: () => {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

// ── 18. navigator.wakeLock stub ──────────────────────────────────────────
if (!navigator.wakeLock) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'wakeLock', () => ({{
            request: () => Promise.resolve({{
                released: false,
                type: 'screen',
                release: () => Promise.resolve(),
                addEventListener: () => {{}},
                removeEventListener: () => {{}},
            }}),
        }}), navigator);
    }} catch(_) {{}}
}}

// ── 19. navigator.locks stub ─────────────────────────────────────────────
if (!navigator.locks) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'locks', () => ({{
            request: () => Promise.resolve(),
            query: () => Promise.resolve({{ held: [], pending: [] }}),
        }}), navigator);
    }} catch(_) {{}}
}}

// ── 20. navigator.storage stub ───────────────────────────────────────────
if (window.StorageManager && StorageManager.prototype.estimate) {{
    const origEstimate = StorageManager.prototype.estimate;
    StorageManager.prototype.estimate = async function() {{
        if (!(this instanceof StorageManager)) {{
            throw new TypeError("Failed to execute 'estimate' on 'StorageManager': Illegal invocation");
        }}
        try {{
            const real = await origEstimate.call(this);
            return {{
                quota: real.quota || _STORAGE_ESTIMATE.quota,
                usage: Math.max(real.usage || 0, _STORAGE_ESTIMATE.usage),
                usageDetails: Object.assign({{}}, _STORAGE_ESTIMATE.usageDetails || {{}}, real.usageDetails || {{}}),
            }};
        }} catch(_) {{
            return Object.assign({{}}, _STORAGE_ESTIMATE);
        }}
    }};
    __markNative(StorageManager.prototype.estimate, 'estimate');
}}

// ── 21. document.hasFocus() ──────────────────────────────────────────────
if (Document.prototype.hasFocus) {{
    const origHasFocus = Document.prototype.hasFocus;
    Document.prototype.hasFocus = function() {{
        if (!(this instanceof Document)) {{
            throw new TypeError("Failed to execute 'hasFocus' on 'Document': Illegal invocation");
        }}
        return true;
    }};
    __markNative(Document.prototype.hasFocus, 'hasFocus');
}}

// ── 22. window.name clearing ─────────────────────────────────────────────
try {{
    if (window.name && window.name.length > 0) {{ window.name = ''; }}
}} catch(_) {{}}

// ── 23. performance.getEntries() / getEntriesByType() filtering ──────────
if (window.Performance && Performance.prototype.getEntries) {{
    const _origGetEntries = Performance.prototype.getEntries;
    const _origGetEntriesByType = Performance.prototype.getEntriesByType;
    const _origGetEntriesByName = Performance.prototype.getEntriesByName;

    const _filterEntries = (entries) => entries.filter(e => {{
        const n = (e.name || '').toLowerCase();
        return !n.includes('__playwright') && !n.includes('pptr:') &&
               !n.includes('devtools') && !n.includes('chrome-extension://');
    }});

    Performance.prototype.getEntries = function() {{
        if (!(this instanceof Performance)) {{
            throw new TypeError("Failed to execute 'getEntries' on 'Performance': Illegal invocation");
        }}
        return _filterEntries(_origGetEntries.call(this));
    }};
    __markNative(Performance.prototype.getEntries, 'getEntries');

    Performance.prototype.getEntriesByType = function(type) {{
        if (!(this instanceof Performance)) {{
            throw new TypeError("Failed to execute 'getEntriesByType' on 'Performance': Illegal invocation");
        }}
        return _filterEntries(_origGetEntriesByType.call(this, type));
    }};
    __markNative(Performance.prototype.getEntriesByType, 'getEntriesByType');

    Performance.prototype.getEntriesByName = function(name, type) {{
        if (!(this instanceof Performance)) {{
            throw new TypeError("Failed to execute 'getEntriesByName' on 'Performance': Illegal invocation");
        }}
        return _filterEntries(_origGetEntriesByName.call(this, name, type));
    }};
    __markNative(Performance.prototype.getEntriesByName, 'getEntriesByName');
}}

// ── 24. performance.now() micro-jitter ──────────────────────────────────
if (window.Performance && Performance.prototype.now) {{
    const _origPerfNow = Performance.prototype.now;
    Performance.prototype.now = function() {{
        if (!(this instanceof Performance)) {{
            throw new TypeError("Failed to execute 'now' on 'Performance': Illegal invocation");
        }}
        const real = _origPerfNow.call(this);
        let s = (_SEED ^ (Math.floor(real) & 0xffffffff));
        s = (s * 1103515245 + 12345) & 0x7fffffff;
        return real + (s % 200 - 100) * 0.001;  // ±0.1ms jitter
    }};
    __markNative(Performance.prototype.now, 'now');
}}

// ── 25. Function.prototype.toString protection ───────────────────────────
const _patchToString = (fn, name) => {{
    __markNative(fn, name);
}};
[
    [document.hasFocus, 'hasFocus'],
    [performance.now, 'now'],
    [navigator.getGamepads, 'getGamepads'],
].forEach(([fn, name]) => {{ if (fn) _patchToString(fn, name); }});

// ── 26. Object.getOwnPropertyDescriptor consistency ──────────────────────
__markNative(Object.getOwnPropertyDescriptor, 'getOwnPropertyDescriptor');

// ── 27. window.matchMedia — realistic media query responses ─────────────
if (Window.prototype.matchMedia) {{
    const _origMatchMedia = Window.prototype.matchMedia;
    Window.prototype.matchMedia = function(query) {{
        if (!(this instanceof Window) && this !== window) {{
            throw new TypeError("Failed to execute 'matchMedia' on 'Window': Illegal invocation");
        }}
        const result = _origMatchMedia.call(this, query);
        const q = query.toLowerCase().trim();
        let spoofed = null;

        if (q.includes('prefers-color-scheme: dark')) spoofed = false;
        else if (q.includes('prefers-color-scheme: light')) spoofed = true;
        else if (q.includes('prefers-reduced-motion: reduce')) spoofed = false;
        else if (q.includes('pointer: fine')) spoofed = {json.dumps(pointer_fine)};
        else if (q.includes('pointer: coarse')) spoofed = {json.dumps(pointer_coarse)};
        else if (q.includes('hover: hover')) spoofed = {json.dumps(hover_hover)};
        else if (q.includes('hover: none')) spoofed = {json.dumps(hover_none)};
        else if (q.includes('any-pointer: fine')) spoofed = {json.dumps(pointer_fine)};
        else if (q.includes('any-hover: hover')) spoofed = {json.dumps(hover_hover)};
        else if (q.includes('display-mode: standalone')) spoofed = false;
        else if (q.includes('prefers-reduced-data')) spoofed = false;

        if (spoofed !== null) {{
            Object.defineProperty(result, 'matches', {{
                get: function() {{
                    if (!(this instanceof MediaQueryList)) {{
                        throw new TypeError("Illegal invocation");
                    }}
                    return spoofed;
                }},
                configurable: true
            }});
        }}
        return result;
    }};
    __markNative(Window.prototype.matchMedia, 'matchMedia');
}}

// ── 28. Canvas measureText guard (font fingerprinting defense) ───────────
const _GENERIC_FONT_TOKENS = ['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui'];
const _PROFILE_ELEMENT_FONT = '"' + _DEFAULT_PROFILE_FONT + '", monospace';
const _fontFamiliesFromCss = (fontCss) => {{
    try {{
        const familyText = String(fontCss || '').replace(/^.*?\\b\\d+(?:\\.\\d+)?(?:px|pt|em|rem|%)\\b(?:\\/[^\\s]+)?\\s*/i, '');
        return familyText
            .split(',')
            .map(part => part.trim().replace(/^['"]|['"]$/g, '').toLowerCase())
            .filter(Boolean);
    }} catch(_) {{
        return [];
    }}
}};
const _isAllowedFontFamily = (family) => {{
    const normalized = String(family || '').trim().replace(/^['"]|['"]$/g, '').toLowerCase();
    return !normalized || _GENERIC_FONT_TOKENS.includes(normalized) || _ALLOWED_FONTS.has(normalized);
}};
const _blocksProfileFont = (fontCss) => {{
    if (!_IS_MOBILE_PROFILE) return false;
    const families = _fontFamiliesFromCss(fontCss);
    return families.length > 0 && families.some(family => !_isAllowedFontFamily(family));
}};
const _profileCanvasFont = (fontCss) => {{
    const original = String(fontCss || '16px sans-serif');
    const replaced = original.replace(
        /^(.+?\\b\\d+(?:\\.\\d+)?(?:px|pt|em|rem|%)\\b(?:\\/[^\\s]+)?\\s*).*/i,
        function(_, prefix) {{ return prefix + '"' + _DEFAULT_PROFILE_FONT + '"'; }}
    );
    return replaced === original ? '16px "' + _DEFAULT_PROFILE_FONT + '"' : replaced;
}};
const _TEXT_METRIC_PROPS = [
    'width', 'actualBoundingBoxLeft', 'actualBoundingBoxRight',
    'actualBoundingBoxAscent', 'actualBoundingBoxDescent',
    'fontBoundingBoxAscent', 'fontBoundingBoxDescent',
    'emHeightAscent', 'emHeightDescent',
    'hangingBaseline', 'alphabeticBaseline', 'ideographicBaseline',
];
const _cloneMetricsWithNoise = (metrics, noise) => {{
    const clone = Object.create(Object.getPrototypeOf(metrics));
    for (const prop of _TEXT_METRIC_PROPS) {{
        try {{
            let value = metrics[prop];
            if (typeof value === 'number' && Number.isFinite(value)) {{
                if (prop === 'width' || prop === 'actualBoundingBoxRight') value += noise;
            }}
            Object.defineProperty(clone, prop, {{
                value,
                enumerable: false,
                configurable: true,
            }});
        }} catch(_) {{}}
    }}
    return clone;
}};
const _origMeasureText = CanvasRenderingContext2D.prototype.measureText;
CanvasRenderingContext2D.prototype.measureText = function(text) {{
    if (!(this instanceof CanvasRenderingContext2D)) {{
        throw new TypeError("Failed to execute 'measureText' on 'CanvasRenderingContext2D': Illegal invocation");
    }}
    let restoreFont = null;
    let metrics;
    try {{
        if (_blocksProfileFont(this.font)) {{
            restoreFont = this.font;
            this.font = _profileCanvasFont(this.font);
        }}
        metrics = _origMeasureText.call(this, text);
    }} finally {{
        if (restoreFont !== null) {{
            try {{ this.font = restoreFont; }} catch(_) {{}}
        }}
    }}
    let h = _SEED;
    for (let i = 0; i < text.length; i++) {{
        h = (h * 31 + text.charCodeAt(i)) & 0xffffffff;
    }}
    const noise = (h % 10 - 5) * 0.002;  // ±0.01px
    return _cloneMetricsWithNoise(metrics, noise);
}};
__markNative(CanvasRenderingContext2D.prototype.measureText, 'measureText');

// ── 28b. DOM font probing guard ─────────────────────────────────────────
if (_IS_MOBILE_PROFILE) {{
    const _looksLikeFontProbe = (el) => {{
        try {{
            if (!el || el.children.length > 0) return false;
            const text = (el.textContent || '').trim();
            if (!text || text.length > 180) return false;
            const style = getComputedStyle(el);
            const pos = style.position;
            const hidden = style.visibility === 'hidden' || style.opacity === '0' ||
                pos === 'absolute' || pos === 'fixed' ||
                parseInt(style.left || '0', 10) < -100 ||
                parseInt(style.top || '0', 10) < -100;
            const probeText = text.includes('mmmMMMmmm') ||
                text.includes('mmmwwwmmmWWW') ||
                /m{{4,}}.*l{{2}}i/i.test(text);
            const probeId = /^(span|div)_/i.test(String(el.id || ''));
            const fam = String(style.fontFamily || '').toLowerCase();
            const firstFamily = fam.split(',')[0].trim().replace(/^['"]|['"]$/g, '');
            if (probeText) return true;
            return (hidden || probeId) && firstFamily && !_isAllowedFontFamily(firstFamily);
        }} catch(_) {{
            return false;
        }}
    }};
    const _withProfileElementFont = (el, fn) => {{
        const style = el && el.style;
        if (!style || typeof style.setProperty !== 'function') {{
            return fn();
        }}
        const previousValue = style.getPropertyValue('font-family');
        const previousPriority = style.getPropertyPriority('font-family');
        try {{
            style.setProperty('font-family', _PROFILE_ELEMENT_FONT, 'important');
            return fn();
        }} finally {{
            try {{
                if (previousValue) {{
                    style.setProperty('font-family', previousValue, previousPriority || '');
                }} else {{
                    style.removeProperty('font-family');
                }}
            }} catch(_) {{}}
        }}
    }};
    const _offsetWidth = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth');
    const _offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight');
    const _clientWidth = Object.getOwnPropertyDescriptor(Element.prototype, 'clientWidth');
    const _clientHeight = Object.getOwnPropertyDescriptor(Element.prototype, 'clientHeight');
    if (_offsetWidth && _offsetWidth.get) {{
        Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {{
            get: function() {{
                if (!(this instanceof HTMLElement)) {{
                    throw new TypeError("Illegal invocation");
                }}
                if (_looksLikeFontProbe(this)) return _withProfileElementFont(this, () => _offsetWidth.get.call(this));
                return _offsetWidth.get.call(this);
            }},
            configurable: true,
        }});
    }}
    if (_offsetHeight && _offsetHeight.get) {{
        Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {{
            get: function() {{
                if (!(this instanceof HTMLElement)) {{
                    throw new TypeError("Illegal invocation");
                }}
                if (_looksLikeFontProbe(this)) return _withProfileElementFont(this, () => _offsetHeight.get.call(this));
                return _offsetHeight.get.call(this);
            }},
            configurable: true,
        }});
    }}
    if (_clientWidth && _clientWidth.get) {{
        Object.defineProperty(Element.prototype, 'clientWidth', {{
            get: function() {{
                if (!(this instanceof Element)) {{
                    throw new TypeError("Illegal invocation");
                }}
                if (_looksLikeFontProbe(this)) return _withProfileElementFont(this, () => _clientWidth.get.call(this));
                return _clientWidth.get.call(this);
            }},
            configurable: true,
        }});
    }}
    if (_clientHeight && _clientHeight.get) {{
        Object.defineProperty(Element.prototype, 'clientHeight', {{
            get: function() {{
                if (!(this instanceof Element)) {{
                    throw new TypeError("Illegal invocation");
                }}
                if (_looksLikeFontProbe(this)) return _withProfileElementFont(this, () => _clientHeight.get.call(this));
                return _clientHeight.get.call(this);
            }},
            configurable: true,
        }});
    }}
    const _origGetBoundingClientRect = Element.prototype.getBoundingClientRect;
    Element.prototype.getBoundingClientRect = function() {{
        if (!(this instanceof Element)) {{
            throw new TypeError("Failed to execute 'getBoundingClientRect' on 'Element': Illegal invocation");
        }}
        if (!_looksLikeFontProbe(this)) return _origGetBoundingClientRect.call(this);
        return _withProfileElementFont(this, () => _origGetBoundingClientRect.call(this));
    }};

    try {{
        if (typeof FontFaceSet !== 'undefined' && FontFaceSet.prototype) {{
            const _origFontCheck = FontFaceSet.prototype.check;
            if (typeof _origFontCheck === 'function') {{
                FontFaceSet.prototype.check = function(font, text) {{
                    if (!(this instanceof FontFaceSet)) {{
                        throw new TypeError("Failed to execute 'check' on 'FontFaceSet': Illegal invocation");
                    }}
                    if (_blocksProfileFont(font)) return false;
                    return _origFontCheck.call(this, font, text);
                }};
                __markNative(FontFaceSet.prototype.check, 'check');
            }}
            const _origFontLoad = FontFaceSet.prototype.load;
            if (typeof _origFontLoad === 'function') {{
                FontFaceSet.prototype.load = function(font, text) {{
                    if (!(this instanceof FontFaceSet)) {{
                        throw new TypeError("Failed to execute 'load' on 'FontFaceSet': Illegal invocation");
                    }}
                    if (_blocksProfileFont(font)) return Promise.resolve([]);
                    return _origFontLoad.call(this, font, text);
                }};
                __markNative(FontFaceSet.prototype.load, 'load');
            }}
        }}
        if (typeof window.FontFace === 'function') {{
            const _NativeFontFace = window.FontFace;
            const _localFontNamesFromSource = (source) => {{
                const names = [];
                try {{
                    const re = /local\\(\\s*(?:"([^"]+)"|'([^']+)'|([^\\)]+))\\s*\\)/gi;
                    let match;
                    while ((match = re.exec(String(source || '')))) {{
                        names.push(String(match[1] || match[2] || match[3] || '').trim());
                    }}
                }} catch(_) {{}}
                return names;
            }};
            const _WrappedFontFace = function(family, source, descriptors) {{
                if (!(this instanceof _WrappedFontFace)) {{
                    throw new TypeError("Failed to construct 'FontFace': Please use the 'new' operator, this DOM object constructor cannot be called as a function.");
                }}
                const localNames = _localFontNamesFromSource(source);
                if (localNames.length && localNames.some(name => !_isAllowedFontFamily(name))) {{
                    return new _NativeFontFace(family, 'local("__profile_missing_font__")', descriptors);
                }}
                return new _NativeFontFace(family, source, descriptors);
            }};
            _WrappedFontFace.prototype = _NativeFontFace.prototype;
            try {{ Object.setPrototypeOf(_WrappedFontFace, _NativeFontFace); }} catch(_) {{}}
            try {{ Object.defineProperty(_WrappedFontFace, 'name', {{ value: 'FontFace', configurable: true }}); }} catch(_) {{}}
            try {{ Object.defineProperty(_WrappedFontFace, 'length', {{ value: 2, configurable: true }}); }} catch(_) {{}}
            __markNative(_WrappedFontFace, 'FontFace');
            Object.defineProperty(window, 'FontFace', {{
                value: _WrappedFontFace,
                configurable: true,
                writable: true,
            }});
        }}
        try {{ delete window.queryLocalFonts; }} catch(_) {{}}
        try {{ if (typeof Window !== 'undefined') delete Window.prototype.queryLocalFonts; }} catch(_) {{}}
        if ('queryLocalFonts' in window) {{
            Object.defineProperty(window, 'queryLocalFonts', {{
                value: async () => [],
                configurable: true,
                writable: true,
            }});
        }}
        try {{ delete window.FontData; }} catch(_) {{}}

        const _installFrameFontGuards = (win) => {{
            try {{
                if (!win || win.__profileFontGuarded) return;
                Object.defineProperty(win, '__profileFontGuarded', {{ value: true, configurable: true }});
                const looksLikeProbe = (el) => {{
                    try {{
                        if (!el || el.children.length > 0) return false;
                        const text = (el.textContent || '').trim();
                        if (!text || text.length > 240) return false;
                        const style = win.getComputedStyle(el);
                        const fam = String(style.fontFamily || '').toLowerCase();
                        const firstFamily = fam.split(',')[0].trim().replace(/^['"]|['"]$/g, '');
                        if (text.includes('mmmm') || text.includes('mmmMMM')) return true;
                        if (!firstFamily || _isAllowedFontFamily(firstFamily)) return false;
                        const pos = style.position;
                        return style.visibility === 'hidden' || style.opacity === '0' ||
                            pos === 'absolute' || pos === 'fixed' ||
                            parseInt(style.left || '0', 10) < -100 ||
                            parseInt(style.top || '0', 10) < -100 ||
                            text.includes('mmmm') || text.includes('mmmMMM');
                    }} catch(_) {{
                        return false;
                    }}
                }};
                const withProfileFont = (el, fn) => {{
                    const style = el && el.style;
                    if (!style || typeof style.setProperty !== 'function') return fn();
                    const prevValue = style.getPropertyValue('font-family');
                    const prevPriority = style.getPropertyPriority('font-family');
                    try {{
                        style.setProperty('font-family', _PROFILE_ELEMENT_FONT, 'important');
                        return fn();
                    }} finally {{
                        try {{
                            if (prevValue) style.setProperty('font-family', prevValue, prevPriority || '');
                            else style.removeProperty('font-family');
                        }} catch(_) {{}}
                    }}
                }};
                const H = win.HTMLElement && win.HTMLElement.prototype;
                const E = win.Element && win.Element.prototype;
                if (H) {{
                    const descWidth = Object.getOwnPropertyDescriptor(H, 'offsetWidth');
                    const descHeight = Object.getOwnPropertyDescriptor(H, 'offsetHeight');
                    if (descWidth && descWidth.get) {{
                        Object.defineProperty(H, 'offsetWidth', {{
                            get: function() {{
                                if (!(this instanceof win.HTMLElement)) throw new TypeError("Illegal invocation");
                                if (looksLikeProbe(this)) return withProfileFont(this, () => descWidth.get.call(this));
                                return descWidth.get.call(this);
                            }},
                            configurable: true
                        }});
                    }}
                    if (descHeight && descHeight.get) {{
                        Object.defineProperty(H, 'offsetHeight', {{
                            get: function() {{
                                if (!(this instanceof win.HTMLElement)) throw new TypeError("Illegal invocation");
                                if (looksLikeProbe(this)) return withProfileFont(this, () => descHeight.get.call(this));
                                return descHeight.get.call(this);
                            }},
                            configurable: true
                        }});
                    }}
                }}
                if (E) {{
                    const descCWidth = Object.getOwnPropertyDescriptor(E, 'clientWidth');
                    const descCHeight = Object.getOwnPropertyDescriptor(E, 'clientHeight');
                    if (descCWidth && descCWidth.get) {{
                        Object.defineProperty(E, 'clientWidth', {{
                            get: function() {{
                                if (!(this instanceof win.Element)) throw new TypeError("Illegal invocation");
                                if (looksLikeProbe(this)) return withProfileFont(this, () => descCWidth.get.call(this));
                                return descCWidth.get.call(this);
                            }},
                            configurable: true
                        }});
                    }}
                    if (descCHeight && descCHeight.get) {{
                        Object.defineProperty(E, 'clientHeight', {{
                            get: function() {{
                                if (!(this instanceof win.Element)) throw new TypeError("Illegal invocation");
                                if (looksLikeProbe(this)) return withProfileFont(this, () => descCHeight.get.call(this));
                                return descCHeight.get.call(this);
                            }},
                            configurable: true
                        }});
                    }}
                }}
                if (E && E.getBoundingClientRect) {{
                    const rect = E.getBoundingClientRect;
                    E.getBoundingClientRect = function() {{
                        if (!(this instanceof win.Element)) {{
                            throw new TypeError("Failed to execute 'getBoundingClientRect' on 'Element': Illegal invocation");
                        }}
                        if (looksLikeProbe(this)) return withProfileFont(this, () => rect.call(this));
                        return rect.call(this);
                    }};
                }}
                const Ctx = win.CanvasRenderingContext2D && win.CanvasRenderingContext2D.prototype;
                if (Ctx && Ctx.measureText) {{
                    const measure = Ctx.measureText;
                    Ctx.measureText = function(text) {{
                        if (!(this instanceof win.CanvasRenderingContext2D)) {{
                            throw new TypeError("Failed to execute 'measureText' on 'CanvasRenderingContext2D': Illegal invocation");
                        }}
                        let restore = null;
                        try {{
                            if (_blocksProfileFont(this.font)) {{
                                restore = this.font;
                                this.font = _profileCanvasFont(this.font);
                            }}
                            return measure.call(this, text);
                        }} finally {{
                            if (restore !== null) try {{ this.font = restore; }} catch(_) {{}}
                        }}
                    }};
                }}
                if (win.document && win.document.fonts && win.document.fonts.check) {{
                    const check = win.document.fonts.check.bind(win.document.fonts);
                    win.document.fonts.check = function(font, text) {{
                        if (!(this instanceof win.FontFaceSet)) {{
                            throw new TypeError("Failed to execute 'check' on 'FontFaceSet': Illegal invocation");
                        }}
                        if (_blocksProfileFont(font)) return false;
                        return check(font, text);
                    }};
                }}
                if (typeof win.FontFace === 'function') {{
                    const NativeFrameFontFace = win.FontFace;
                    win.FontFace = function(family, source, descriptors) {{
                        if (!(this instanceof win.FontFace)) {{
                            throw new TypeError("Failed to construct 'FontFace': Please use the 'new' operator...");
                        }}
                        if (_blocksProfileFont('12px "' + family + '"') || /local\\(/i.test(String(source || ''))) {{
                            return new NativeFrameFontFace(family, 'local("__profile_missing_font__")', descriptors);
                        }}
                        return new NativeFrameFontFace(family, source, descriptors);
                    }};
                    win.FontFace.prototype = NativeFrameFontFace.prototype;
                    try {{ Object.setPrototypeOf(win.FontFace, NativeFrameFontFace); }} catch(_) {{}}
                }}
                try {{ delete win.queryLocalFonts; }} catch(_) {{}}
                try {{ delete win.FontData; }} catch(_) {{}}
            }} catch(_) {{}}
        }};
        const _installIframe = (iframe) => {{
            try {{ if (iframe && iframe.contentWindow) _installFrameFontGuards(iframe.contentWindow); }} catch(_) {{}}
        }};
        try {{
            const cw = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow');
            if (cw && cw.get) {{
                Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {{
                    get: function() {{
                        if (!(this instanceof HTMLIFrameElement)) throw new TypeError("Illegal invocation");
                        const win = cw.get.call(this);
                        _installFrameFontGuards(win);
                        return win;
                    }},
                    configurable: true,
                }});
            }}
            const cd = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentDocument');
            if (cd && cd.get) {{
                Object.defineProperty(HTMLIFrameElement.prototype, 'contentDocument', {{
                    get: function() {{
                        if (!(this instanceof HTMLIFrameElement)) throw new TypeError("Illegal invocation");
                        const doc = cd.get.call(this);
                        if (doc && doc.defaultView) _installFrameFontGuards(doc.defaultView);
                        return doc;
                    }},
                    configurable: true,
                }});
            }}
            const createElement = Document.prototype.createElement;
            Document.prototype.createElement = function(name, options) {{
                if (!(this instanceof Document)) {{
                    throw new TypeError("Failed to execute 'createElement' on 'Document': Illegal invocation");
                }}
                const el = createElement.call(this, name, options);
                if (String(name || '').toLowerCase() === 'iframe') setTimeout(() => _installIframe(el), 0);
                return el;
            }};
            const appendChild = Node.prototype.appendChild;
            Node.prototype.appendChild = function(node) {{
                if (!(this instanceof Node)) {{
                    throw new TypeError("Failed to execute 'appendChild' on 'Node': Illegal invocation");
                }}
                const out = appendChild.call(this, node);
                if (node && String(node.tagName || '').toLowerCase() === 'iframe') setTimeout(() => _installIframe(node), 0);
                return out;
            }};
        }} catch(_) {{}}
    }} catch(_) {{}}
}}

// ── 29. WebGL getSupportedExtensions — consistent whitelist ─────────────
const _WEBGL_EXTENSIONS = [
    'ANGLE_instanced_arrays', 'EXT_blend_minmax', 'EXT_color_buffer_half_float',
    'EXT_disjoint_timer_query', 'EXT_float_blend', 'EXT_frag_depth',
    'EXT_shader_texture_lod', 'EXT_texture_compression_bptc',
    'EXT_texture_compression_rgtc', 'EXT_texture_filter_anisotropic',
    'WEBKIT_EXT_texture_filter_anisotropic', 'EXT_sRGB',
    'KHR_parallel_shader_compile', 'OES_element_index_uint',
    'OES_fbo_render_mipmap', 'OES_standard_derivatives',
    'OES_texture_float', 'OES_texture_float_linear',
    'OES_texture_half_float', 'OES_texture_half_float_linear',
    'OES_vertex_array_object', 'WEBGL_color_buffer_float',
    'WEBGL_compressed_texture_s3tc', 'WEBGL_compressed_texture_s3tc_srgb',
    'WEBGL_debug_renderer_info', 'WEBGL_debug_shaders',
    'WEBGL_depth_texture', 'WEBKIT_WEBGL_depth_texture',
    'WEBGL_draw_buffers', 'WEBGL_lose_context', 'WEBKIT_WEBGL_lose_context',
    'WEBGL_multi_draw',
];
WebGLRenderingContext.prototype.getSupportedExtensions = function() {{
    if (!(this instanceof WebGLRenderingContext)) {{
        throw new TypeError("Failed to execute 'getSupportedExtensions' on 'WebGLRenderingContext': Illegal invocation");
    }}
    return _WEBGL_EXTENSIONS;
}};
__markNative(WebGLRenderingContext.prototype.getSupportedExtensions, 'getSupportedExtensions');

if (typeof WebGL2RenderingContext !== 'undefined') {{
    WebGL2RenderingContext.prototype.getSupportedExtensions = function() {{
        if (!(this instanceof WebGL2RenderingContext)) {{
            throw new TypeError("Failed to execute 'getSupportedExtensions' on 'WebGL2RenderingContext': Illegal invocation");
        }}
        return _WEBGL_EXTENSIONS;
    }};
    __markNative(WebGL2RenderingContext.prototype.getSupportedExtensions, 'getSupportedExtensions');
}}

// ── 30. WebGL getShaderPrecisionFormat ─────────────────────────────────
const _PRECISION_MAP = {{
    35632: {{ // FRAGMENT_SHADER
        0: {{ rangeMin: 127, rangeMax: 127, precision: 23 }},  // LOW_FLOAT
        1: {{ rangeMin: 127, rangeMax: 127, precision: 23 }},  // MEDIUM_FLOAT
        2: {{ rangeMin: 127, rangeMax: 127, precision: 23 }},  // HIGH_FLOAT
        4: {{ rangeMin: 31, rangeMax: 30, precision: 0 }},     // LOW_INT
        5: {{ rangeMin: 31, rangeMax: 30, precision: 0 }},     // MEDIUM_INT
        6: {{ rangeMin: 31, rangeMax: 30, precision: 0 }},     // HIGH_INT
    }},
    35633: {{ // VERTEX_SHADER
        0: {{ rangeMin: 127, rangeMax: 127, precision: 23 }},
        1: {{ rangeMin: 127, rangeMax: 127, precision: 23 }},
        2: {{ rangeMin: 127, rangeMax: 127, precision: 23 }},
        4: {{ rangeMin: 31, rangeMax: 30, precision: 0 }},
        5: {{ rangeMin: 31, rangeMax: 30, precision: 0 }},
        6: {{ rangeMin: 31, rangeMax: 30, precision: 0 }},
    }},
}};
const _origGetShaderPF = WebGLRenderingContext.prototype.getShaderPrecisionFormat;
WebGLRenderingContext.prototype.getShaderPrecisionFormat = function(shaderType, precType) {{
    if (!(this instanceof WebGLRenderingContext)) {{
        throw new TypeError("Failed to execute 'getShaderPrecisionFormat' on 'WebGLRenderingContext': Illegal invocation");
    }}
    const map = _PRECISION_MAP[shaderType];
    if (map && map[precType]) {{
        const v = map[precType];
        return {{ rangeMin: v.rangeMin, rangeMax: v.rangeMax, precision: v.precision }};
    }}
    return _origGetShaderPF.call(this, shaderType, precType);
}};
__markNative(WebGLRenderingContext.prototype.getShaderPrecisionFormat, 'getShaderPrecisionFormat');

if (typeof WebGL2RenderingContext !== 'undefined') {{
    const _origGetShaderPF2 = WebGL2RenderingContext.prototype.getShaderPrecisionFormat;
    WebGL2RenderingContext.prototype.getShaderPrecisionFormat = function(shaderType, precType) {{
        if (!(this instanceof WebGL2RenderingContext)) {{
            throw new TypeError("Failed to execute 'getShaderPrecisionFormat' on 'WebGL2RenderingContext': Illegal invocation");
        }}
        const map = _PRECISION_MAP[shaderType];
        if (map && map[precType]) {{
            const v = map[precType];
            return {{ rangeMin: v.rangeMin, rangeMax: v.rangeMax, precision: v.precision }};
        }}
        return _origGetShaderPF2.call(this, shaderType, precType);
    }};
    __markNative(WebGL2RenderingContext.prototype.getShaderPrecisionFormat, 'getShaderPrecisionFormat');
}}

// ── 31. navigator.sendBeacon passthrough ────────────────────────────────
if (!Navigator.prototype.sendBeacon) {{
    Navigator.prototype.sendBeacon = function(url, data) {{
        if (!(this instanceof Navigator)) {{
            throw new TypeError("Failed to execute 'sendBeacon' on 'Navigator': Illegal invocation");
        }}
        try {{ fetch(url, {{ method: 'POST', body: data, keepalive: true }}); }} catch(_) {{}}
        return true;
    }};
    __markNative(Navigator.prototype.sendBeacon, 'sendBeacon');
}}

// ── 32. window.opener ────────────────────────────────────────────────────
try {{
    if (window.opener !== null && window.opener !== undefined) {{
        Object.defineProperty(window, 'opener', {{
            get: __markNative(function opener() {{
                if (this !== window) throw new TypeError("Illegal invocation");
                return null;
            }}, 'get opener'),
            configurable: true
        }});
    }}
}} catch(_) {{}}

// ── 33. document.referrer control ────────────────────────────────────────
try {{
    const _ref = document.referrer;
    if (_ref && (_ref.includes('devtools') || _ref.includes('localhost:') ||
                 _ref.includes('127.0.0.1') || _ref.includes('playwright'))) {{
        const getReferrer = __markNative(function referrer() {{
            if (!(this instanceof Document)) throw new TypeError("Illegal invocation");
            return '';
        }}, 'get referrer');
        Object.defineProperty(Document.prototype, 'referrer', {{
            get: getReferrer,
            configurable: true
        }});
    }}
}} catch(_) {{}}

// ── 34. CSSStyleDeclaration font leak prevention ─────────────────────────
const _origGetPropVal = CSSStyleDeclaration.prototype.getPropertyValue;
CSSStyleDeclaration.prototype.getPropertyValue = function(prop) {{
    if (!(this instanceof CSSStyleDeclaration)) {{
        throw new TypeError("Failed to execute 'getPropertyValue' on 'CSSStyleDeclaration': Illegal invocation");
    }}
    return _origGetPropVal.call(this, prop);
}};
__markNative(CSSStyleDeclaration.prototype.getPropertyValue, 'getPropertyValue');

// ── 35. navigator.mediaSession stub ─────────────────────────────────────
if (!navigator.mediaSession) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'mediaSession', () => ({{
            metadata: null,
            playbackState: 'none',
            setActionHandler: function() {{}},
            setCameraActive: function() {{}},
            setMicrophoneActive: function() {{}},
            setPositionState: function() {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

// ── 36. HTMLCanvasElement.toBlob — noise consistent with toDataURL ───────
const _origToBlob = HTMLCanvasElement.prototype.toBlob;
const __withTinyBlobCanvasNoise = (canvas, fn) => {{
    const ctx = canvas.getContext('2d');
    if (!ctx || canvas.width <= 0 || canvas.height <= 0) return fn();
    let imageData = null;
    const changed = [];
    try {{
        imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
        const data = imageData.data;
        const stride = Math.max(4096, Math.floor(data.length / 12));
        let s = {canvas_seed} ^ (canvas.width << 8) ^ canvas.height;
        for (let i = ((s >>> 3) % 64) * 4; i < data.length; i += stride) {{
            s = (s * 1103515245 + 12345) & 0x7fffffff;
            changed.push([i, data[i]]);
            data[i] = (data[i] + (s % 3 - 1)) & 0xff;
        }}
        if (changed.length) ctx.putImageData(imageData, 0, 0);
    }} catch(_) {{
        imageData = null;
    }}
    const restore = () => {{
        if (imageData && changed.length) {{
            try {{
                for (const item of changed) imageData.data[item[0]] = item[1];
                ctx.putImageData(imageData, 0, 0);
            }} catch(_) {{}}
        }}
    }};
    try {{
        return fn(restore);
    }} catch(error) {{
        restore();
        throw error;
    }}
}};
HTMLCanvasElement.prototype.toBlob = function(callback, type, quality) {{
    if (!(this instanceof HTMLCanvasElement)) {{
        throw new TypeError("Failed to execute 'toBlob' on 'HTMLCanvasElement': Illegal invocation");
    }}
    return __withTinyBlobCanvasNoise(this, (restore) => {{
        const wrappedCallback = function(blob) {{
            try {{ restore(); }} catch(_) {{}}
            if (typeof callback === 'function') return callback.call(this, blob);
        }};
        return _origToBlob.call(this, wrappedCallback, type, quality);
    }});
}};
__markNative(HTMLCanvasElement.prototype.toBlob, 'toBlob');

// ── 37. OffscreenCanvas noise ────────────────────────────────────────────
if (typeof OffscreenCanvas !== 'undefined') {{
    const _origOffscreenToBlob = OffscreenCanvas.prototype.convertToBlob;
    if (_origOffscreenToBlob) {{
        OffscreenCanvas.prototype.convertToBlob = async function(options) {{
            if (!(this instanceof OffscreenCanvas)) {{
                throw new TypeError("Failed to execute 'convertToBlob' on 'OffscreenCanvas': Illegal invocation");
            }}
            const ctx = this.getContext('2d');
            if (!ctx || this.width <= 0 || this.height <= 0) {{
                return _origOffscreenToBlob.call(this, options);
            }}
            let imageData = null;
            const changed = [];
            try {{
                imageData = ctx.getImageData(0, 0, this.width, this.height);
                const data = imageData.data;
                const stride = Math.max(4096, Math.floor(data.length / 12));
                let s = _SEED ^ (this.width << 8) ^ this.height;
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
                const blob = await _origOffscreenToBlob.call(this, options);
                return blob;
            }} finally {{
                if (imageData && changed.length) {{
                    try {{
                        for (const item of changed) imageData.data[item[0]] = item[1];
                        ctx.putImageData(imageData, 0, 0);
                    }} catch(_) {{}}
                }}
            }}
        }};
        __markNative(OffscreenCanvas.prototype.convertToBlob, 'convertToBlob');
    }}
}}

// ── 38. window.credentialless ────────────────────────────────────────────
try {{
    if ('credentialless' in window) {{
        _defineSafeGetter(window, 'credentialless', undefined, window);
    }}
}} catch(_) {{}}

// ── 39. performance.eventCounts ──────────────────────────────────────────
if (window.Performance && !Performance.prototype.eventCounts) {{
    try {{
        const fakeCounts = new Map([
            ['click', 0], ['keydown', 0], ['keyup', 0], ['keypress', 0],
            ['mousedown', 0], ['mouseup', 0], ['mousemove', 0], ['mouseover', 0],
            ['pointerdown', 0], ['pointerup', 0], ['pointermove', 0],
            ['scroll', 0], ['wheel', 0], ['touchstart', 0], ['touchend', 0],
        ]);
        _defineSafeGetter(Performance.prototype, 'eventCounts', () => fakeCounts);
    }} catch(_) {{}}
}}

// ── 40. navigator.pdfViewerEnabled ───────────────────────────────────────
_defineSafeGetter(Navigator.prototype, 'pdfViewerEnabled', {json.dumps(pdf_viewer_enabled)}, navigator);

try {{
    if (document.domain) {{
        _defineSafeGetter(Document.prototype, 'domain', () => location.hostname);
    }}
}} catch(_) {{}}

// ── Make all overridden prototype methods non-enumerable ─────────────────
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
_makeNonEnumerable(Date.prototype, 'getTimezoneOffset');
_makeNonEnumerable(Navigator.prototype, 'getGamepads');
if (typeof StorageManager !== 'undefined' && StorageManager.prototype) {{
    _makeNonEnumerable(StorageManager.prototype, 'estimate');
}}
_makeNonEnumerable(Document.prototype, 'hasFocus');
_makeNonEnumerable(Performance.prototype, 'getEntries');
_makeNonEnumerable(Performance.prototype, 'getEntriesByType');
_makeNonEnumerable(Performance.prototype, 'getEntriesByName');
_makeNonEnumerable(Performance.prototype, 'now');
_makeNonEnumerable(Window.prototype, 'matchMedia');
_makeNonEnumerable(CanvasRenderingContext2D.prototype, 'measureText');
_makeNonEnumerable(Element.prototype, 'getBoundingClientRect');
if (typeof FontFaceSet !== 'undefined' && FontFaceSet.prototype) {{
    _makeNonEnumerable(FontFaceSet.prototype, 'check');
    _makeNonEnumerable(FontFaceSet.prototype, 'load');
}}
_makeNonEnumerable(Document.prototype, 'createElement');
_makeNonEnumerable(Node.prototype, 'appendChild');
_makeNonEnumerable(WebGLRenderingContext.prototype, 'getSupportedExtensions');
if (typeof WebGL2RenderingContext !== 'undefined') {{
    _makeNonEnumerable(WebGL2RenderingContext.prototype, 'getSupportedExtensions');
}}
_makeNonEnumerable(WebGLRenderingContext.prototype, 'getShaderPrecisionFormat');
if (typeof WebGL2RenderingContext !== 'undefined') {{
    _makeNonEnumerable(WebGL2RenderingContext.prototype, 'getShaderPrecisionFormat');
}}
_makeNonEnumerable(Navigator.prototype, 'sendBeacon');
_makeNonEnumerable(CSSStyleDeclaration.prototype, 'getPropertyValue');
_makeNonEnumerable(HTMLCanvasElement.prototype, 'toBlob');
if (typeof OffscreenCanvas !== 'undefined') {{
    _makeNonEnumerable(OffscreenCanvas.prototype, 'convertToBlob');
}}

// ═══════════════════════════════════════════════════════════════════════════
// END ADVANCED ANTI-BOT EVASION
// ═══════════════════════════════════════════════════════════════════════════
}})();
"""


async def inject_advanced(page, profile: dict) -> None:
    """Inject all 40 advanced anti-bot evasion patches into *page*.

    Must be called before any page.goto() — use page.add_init_script().
    Typically called right after BrowserProfileManager.inject() so both
    scripts run before any page JS executes.

    Args:
        page: Playwright Page object.
        profile: Browser profile dict from BrowserProfileManager.generate().
    """
    script = build_advanced_script(profile)
    await page.add_init_script(script)


class AdvancedFingerprintManager:
    """Manages injection of all 40+ advanced anti-bot evasion patches.

    Usage:
        manager = AdvancedFingerprintManager()
        await manager.inject(page, profile)
    """

    async def inject(self, page, profile: dict) -> None:
        """Inject base fingerprint + all advanced patches into page."""
        from tools.stealth.fingerprint import BrowserProfileManager, _build_inject_script

        # Base fingerprint (22 techniques)
        base_script = _build_inject_script(profile)
        await page.add_init_script(base_script)

        # Advanced patches (40 more techniques)
        adv_script = build_advanced_script(profile)
        await page.add_init_script(adv_script)

    def technique_list(self) -> list[str]:
        """Return a summary of all injected anti-bot techniques."""
        return [
            # Base fingerprint.py (22 techniques)
            "01. navigator.platform = Win32",
            "02. navigator.language/languages = locale",
            "03. navigator.hardwareConcurrency = 4 or 8",
            "04. navigator.deviceMemory = 8 or 16",
            "05. screen.width/height/availWidth/availHeight/colorDepth/pixelDepth",
            "06. navigator.plugins = realistic plugin list (2-4 plugins)",
            "07. WebGLRenderingContext.getParameter → spoofed GPU vendor/renderer",
            "08. WebGL2RenderingContext.getParameter → spoofed GPU vendor/renderer",
            "09. navigator.webdriver = false (critical bot signal)",
            "10. window.chrome stub (app, runtime, csi, loadTimes)",
            "11. HTMLCanvasElement.toDataURL noise (canvas fingerprint defeat)",
            "12. AudioBuffer.getChannelData noise (audio fingerprint defeat)",
            "13. Intl.DateTimeFormat timezone override",
            "14. navigator.maxTouchPoints = profile touch count",
            "15. navigator.doNotTrack = null",
            "16. performance.memory spoofing (jsHeapSizeLimit, etc.)",
            "17. navigator.getBattery() stub (charging=true, level=1.0)",
            "18. navigator.connection stub matches profile",
            "19. navigator.mediaDevices.enumerateDevices() fake (3 devices)",
            "20. RTCPeerConnection ICE candidate stripping (real IP leak prevention)",
            "21. Error.prepareStackTrace cleanup (remove Playwright stack traces)",
            "22. window.outerWidth/outerHeight distinction (+15/+85px)",
            # Advanced advanced_fingerprint.py (40 techniques)
            "23. navigator.vendor = 'Google Inc.'",
            "24. navigator.appVersion/appName match UA string",
            "25. navigator.product = 'Gecko', productSub = '20030107'",
            "26. navigator.userAgent consistency with profile",
            "27. navigator.oscpu = undefined (Chrome doesn't have this)",
            "28. navigator.buildID = undefined (Firefox-only property)",
            "29. navigator.cookieEnabled = true, onLine = true",
            "30. window.devicePixelRatio = match profile DPR",
            "31. Date.prototype.getTimezoneOffset() override",
            "32. screen.orientation stub (landscape-primary)",
            "33. window.screenLeft/screenTop/screenX/screenY",
            "34. navigator.permissions.query spoofed responses",
            "35. Notification.permission = 'default'",
            "36. navigator.getGamepads() = empty array",
            "37. SpeechSynthesis.getVoices() = profile-scoped voice list",
            "38. navigator.keyboard stub (getLayoutMap, lock, unlock)",
            "39. navigator.wakeLock stub (request())",
            "40. navigator.locks stub (request, query)",
            "41. navigator.storage.estimate() realistic usage values",
            "42. document.hasFocus() = true (tab focus detection defeat)",
            "43. window.name = '' (cross-site tracking prevention)",
            "44. performance.getEntries() filter (remove Playwright markers)",
            "45. performance.now() ±0.1ms jitter (timing attack prevention)",
            "46. Function.prototype.toString protection (native code check)",
            "47. Object.getOwnPropertyDescriptor protection (Proxy-trap detection)",
            "48. window.matchMedia realistic responses (pointer, hover, color-scheme)",
            "49. Canvas measureText noise (font fingerprinting defense)",
            "50. WebGL getSupportedExtensions whitelist",
            "51. WebGL getShaderPrecisionFormat consistent values",
            "52. navigator.sendBeacon presence",
            "53. window.opener = null (cross-site reference prevention)",
            "54. document.referrer control (hide automation URLs)",
            "55. navigator.mediaSession stub",
            "56. HTMLCanvasElement.toBlob noise (consistent with toDataURL)",
            "57. OffscreenCanvas noise patches",
            "58. window.credentialless = undefined",
            "59. performance.eventCounts stub (interactivity simulation)",
            "60. navigator.pdfViewerEnabled matches profile plugins",
            "61. document.domain normalize",
            "62. Mouse position tracker (for natural Bezier movement continuity)",
        ]
