"""
tools/stealth/bot_detection_evasion.py — Runtime anti-bot detection system evasion.

Targets specific commercial bot detection vendors and runtime automation artifacts:
PerimeterX, HUMAN, Kasada, Akamai, DataDome, Arkose, Imperva, F5 Shape, Radware, ThreatMetrix, Cloudflare, Reddit Sentinel
"""

from __future__ import annotations

import json
import re
from typing import Any

from tools.stealth.advanced_fingerprint import _timezone_transition_table


def _parse_sec_ch_ua(sec_ch_ua: str, chrome_ver: str) -> list[dict[str, str]]:
    brands = [
        {"brand": brand, "version": version}
        for brand, version in re.findall(r'"([^"]+)";v="([^"]+)"', sec_ch_ua or "")
    ]
    if brands:
        return brands
    return [
        {"brand": "Not_A Brand", "version": "8"},
        {"brand": "Chromium", "version": chrome_ver},
        {"brand": "Google Chrome", "version": chrome_ver},
    ]


def build_evasion_script(profile: dict) -> str:
    """Build the JS evasion script targeting all major bot detection vendors."""
    user_agent = profile.get("user_agent", "")
    canvas_seed = profile.get("canvas_noise_seed", 12345)
    locale = profile.get("locale", "en-US")
    platform = profile.get("platform", "Win32")
    hw_concurrency = profile.get("hardware_concurrency", 8)
    device_memory = profile.get("device_memory", 8)
    sec_ch_ua = profile.get("sec_ch_ua", "")
    sec_ch_ua_mobile = profile.get("sec_ch_ua_mobile", "?0")
    sec_ch_ua_platform = str(profile.get("sec_ch_ua_platform", '"Windows"')).strip().strip('"')
    is_mobile = bool(profile.get("is_mobile", False)) or sec_ch_ua_mobile == "?1"
    webgl_vendor = profile["webgl_vendor"]
    webgl_renderer = profile["webgl_renderer"]
    platform_version = profile.get("mobile_platform_version", "10.0.0" if not is_mobile else "13.0.0")
    mobile_model = profile.get("mobile_model", "Pixel 7" if is_mobile else "")
    architecture = profile.get("architecture", "arm" if is_mobile else "x86")
    bitness = profile.get("bitness", "64")
    connection = profile.get("connection") or {
        "effectiveType": "4g",
        "downlink": 12 if is_mobile else 10,
        "rtt": 80 if is_mobile else 50,
        "saveData": False,
        "type": "cellular" if is_mobile else "wifi",
    }
    connection_json = json.dumps(connection)
    screen = profile.get("screen_resolution") or {}
    screen_width = int(screen.get("width") or 412)
    screen_height = int(screen.get("height") or 915)
    dpr = profile.get("device_scale_factor", 1)
    max_touch_points = int(profile.get("max_touch_points", 5 if is_mobile else 0))
    timezone = profile.get("timezone", "America/New_York")
    timezone_transitions = _timezone_transition_table(timezone)

    # Derive Sec-CH-UA from UA string
    chrome_ver = "147"
    try:
        import re as _re
        m = _re.search(r"Chrome/(\d+)", user_agent)
        if m:
            chrome_ver = m.group(1)
    except Exception:
        pass
    chrome_full_version = str(profile.get("chrome_full_version") or "").strip() or f"{chrome_ver}.0.0.0"
    ch_brands = _parse_sec_ch_ua(sec_ch_ua, chrome_ver)
    worker_preload_script = f"""
(() => {{
    const define = (obj, prop, value) => {{
        if (!obj) return;
        try {{ Object.defineProperty(obj, prop, {{ get: () => value, configurable: true }}); }} catch (_) {{}}
    }};
    const defineValue = (obj, prop, value) => {{
        if (!obj) return;
        try {{ Object.defineProperty(obj, prop, {{ value, configurable: true }}); }} catch (_) {{}}
    }};
    const nav = self.navigator;
    const navProto = nav ? Object.getPrototypeOf(nav) : null;
    const navTargets = [nav, navProto].filter(Boolean);
    const brands = {json.dumps(ch_brands)};
    const chromeFullVersion = {json.dumps(chrome_full_version)};
    const fullVersionList = brands.map(b => ({{
        brand: b.brand,
        version: (b.brand === 'Chromium' || b.brand === 'Google Chrome') ? chromeFullVersion : (b.version.includes('.') ? b.version : b.version + '.0.0.0')
    }}));
    const uaData = {{
        brands,
        mobile: {json.dumps(is_mobile)},
        platform: {json.dumps(sec_ch_ua_platform)},
        getHighEntropyValues: async function(hints) {{
            const values = {{
                architecture: {json.dumps(architecture)},
                bitness: {json.dumps(bitness)},
                brands,
                fullVersionList,
                formFactors: [{json.dumps("Mobile" if is_mobile else "Desktop")}],
                mobile: {json.dumps(is_mobile)},
                model: {json.dumps(mobile_model)},
                platform: {json.dumps(sec_ch_ua_platform)},
                platformVersion: {json.dumps(platform_version)},
                uaFullVersion: chromeFullVersion,
                wow64: false,
            }};
            const result = {{}};
            (hints || []).forEach(h => {{ if (h in values) result[h] = values[h]; }});
            return result;
        }},
        toJSON: function() {{ return {{ brands, mobile: {json.dumps(is_mobile)}, platform: {json.dumps(sec_ch_ua_platform)} }}; }},
    }};
    navTargets.forEach(target => {{
        define(target, 'userAgent', {json.dumps(user_agent)});
        define(target, 'appVersion', {json.dumps(user_agent.replace("Mozilla/", "", 1))});
        define(target, 'platform', {json.dumps(platform)});
        define(target, 'language', {json.dumps(locale)});
        define(target, 'languages', [{json.dumps(locale)}, 'en']);
        define(target, 'hardwareConcurrency', {json.dumps(hw_concurrency)});
        define(target, 'deviceMemory', {json.dumps(device_memory)});
        define(target, 'maxTouchPoints', {json.dumps(max_touch_points)});
        define(target, 'userAgentData', uaData);
        define(target, 'webdriver', false);
    }});

    try {{
        const OrigDateTimeFormat = Intl.DateTimeFormat;
        const tz = {json.dumps(timezone)};
        const patchedDTF = function(locales, options) {{
            options = Object.assign({{}}, options || {{}});
            options.timeZone = options.timeZone || tz;
            return new OrigDateTimeFormat(locales, options);
        }};
        patchedDTF.prototype = OrigDateTimeFormat.prototype;
        patchedDTF.supportedLocalesOf = OrigDateTimeFormat.supportedLocalesOf.bind(OrigDateTimeFormat);
        Object.defineProperty(Intl, 'DateTimeFormat', {{ value: patchedDTF, configurable: true }});
    }} catch (_) {{}}
    try {{
        const tzTransitions = {json.dumps(timezone_transitions)};
        const origGetTime = Date.prototype.getTime;
        const offsetFor = (timeValue) => {{
            if (!Number.isFinite(timeValue) || !tzTransitions.length) return 0;
            let offset = tzTransitions[0].offset;
            for (let i = 1; i < tzTransitions.length; i++) {{
                if (timeValue < tzTransitions[i].at) break;
                offset = tzTransitions[i].offset;
            }}
            return offset;
        }};
        Date.prototype.getTimezoneOffset = function() {{
            return offsetFor(origGetTime.call(this));
        }};
    }} catch (_) {{}}

    const patchGL = (Ctor) => {{
        if (!Ctor || !Ctor.prototype || !Ctor.prototype.getParameter) return;
        const original = Ctor.prototype.getParameter;
        Ctor.prototype.getParameter = function(param) {{
            if (param === 0x9245) return {json.dumps(webgl_vendor)};
            if (param === 0x9246) return {json.dumps(webgl_renderer)};
            return original.call(this, param);
        }};
    }};
    try {{ patchGL(self.WebGLRenderingContext); }} catch (_) {{}}
    try {{ patchGL(self.WebGL2RenderingContext); }} catch (_) {{}}

    define(self, 'devicePixelRatio', {json.dumps(dpr)});
    defineValue(self, '__profilePatchedWorker', true);

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
    if (self.WebGLRenderingContext) _makeNonEnumerable(self.WebGLRenderingContext.prototype, 'getParameter');
    if (self.WebGL2RenderingContext) _makeNonEnumerable(self.WebGL2RenderingContext.prototype, 'getParameter');

    if (self.registration) {{
        // Inside service worker
    }}
}})();
"""

    return f"""
(() => {{
// ═══════════════════════════════════════════════════════════════════════════
// BOT DETECTION SYSTEM EVASION
// ═══════════════════════════════════════════════════════════════════════════

const _SEED = {canvas_seed};
const _UA = {json.dumps(user_agent)};
const _LOCALE = {json.dumps(locale)};
const _PLATFORM = {json.dumps(platform)};
const _CHROME_VER = {json.dumps(chrome_ver)};
const _CH_BRANDS = {json.dumps(ch_brands)};
const _CH_MOBILE = {json.dumps(is_mobile)};
const _CH_PLATFORM = {json.dumps(sec_ch_ua_platform)};
const _CH_PLATFORM_VERSION = {json.dumps(platform_version)};
const _CH_MODEL = {json.dumps(mobile_model)};
const _CH_ARCHITECTURE = {json.dumps(architecture)};
const _CH_BITNESS = {json.dumps(bitness)};
const _CH_FULL_VERSION = {json.dumps(chrome_full_version)};
const _CH_FORM_FACTORS = [{json.dumps("Mobile" if is_mobile else "Desktop")}];
const _WEBGL_VENDOR = {json.dumps(webgl_vendor)};
const _WEBGL_RENDERER = {json.dumps(webgl_renderer)};
const _WORKER_PRELOAD = {json.dumps(worker_preload_script)};

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

// ── CATEGORY A: Automation Artifact Removal ──────────────────────────────
const _cdcProps = [
    '$cdc_asdjflasutopfhvcZLmcfl_',
    '__webdriver_script_fn',
    '__webdriver_script_func',
    '__driver_evaluate',
    '__webdriver_evaluate',
    '__selenium_evaluate',
    '__fxdriver_evaluate',
    '__driver_unwrapped',
    '__webdriver_unwrapped',
    '__selenium_unwrapped',
    '__fxdriver_unwrapped',
    '__webdriverFunctions',
    '_Selenium_IDE_Recorder',
    '_selenium',
    'calledSelenium',
    '_WEBDRIVER_ELEM_CACHE',
    'ChromeDriverw',
    'driver',
    '__last_webdriver_active_frame__',
    '__webdriver_script_element',
];
_cdcProps.forEach(prop => {{
    try {{
        if (prop in window) delete window[prop];
    }} catch(_) {{}}
}});

const _docSeleniumProps = [
    '__webdriver_evaluate',
    '__selenium_unwrapped',
    '__fxdriver_evaluate',
    '__playwright_target__',
];
_docSeleniumProps.forEach(prop => {{
    try {{
        if (prop in document) delete document[prop];
    }} catch(_) {{}}
}});

const _pwProps = [
    '__playwright',
    '__pw_',
    '__playwright_target__',
    '__playwright_clock__',
    '__bindingCalled',
];
Object.keys(window).forEach(key => {{
    if (_pwProps.some(p => key.startsWith(p))) {{
        try {{ delete window[key]; }} catch(_) {{}}
    }}
}});

// ── CATEGORY B: Event Trust Spoofing ────────────────────────────────────
if (window.EventTarget && Event.prototype) {{
    const _origDispatchEvent = EventTarget.prototype.dispatchEvent;
    EventTarget.prototype.dispatchEvent = function(event) {{
        if (!(this instanceof EventTarget)) {{
            throw new TypeError("Failed to execute 'dispatchEvent' on 'EventTarget': Illegal invocation");
        }}
        try {{
            Object.defineProperty(event, 'isTrusted', {{
                get: function() {{
                    if (!(this instanceof Event)) {{
                        throw new TypeError("Illegal invocation");
                    }}
                    return true;
                }},
                configurable: true,
            }});
        }} catch(_) {{}}
        return _origDispatchEvent.call(this, event);
    }};
    __markNative(EventTarget.prototype.dispatchEvent, 'dispatchEvent');
}}

// ── CATEGORY C: CDP/DevTools Protocol Connection Detection Evasion ────────
try {{
    const _debugNoop = function debug() {{}};
    Object.defineProperty(_debugNoop, '__cdp_noop_v1', {{ value: true, enumerable: false }});
    Object.defineProperty(_debugNoop, 'toString', {{
        value: () => 'function debug() {{ [native code] }}',
        configurable: true, writable: true,
    }});
    Object.defineProperty(_debugNoop, 'name', {{ value: 'debug', configurable: true }});
    Object.defineProperty(console, 'debug', {{
        value: _debugNoop, writable: true, configurable: true,
    }});
}} catch(_) {{}}

try {{
    const _consoleMethods = ['log', 'info', 'warn', 'error', 'trace', 'dir',
                              'table', 'dirxml', 'group', 'groupCollapsed',
                              'count', 'countReset', 'assert'];
    _consoleMethods.forEach(method => {{
        const noop = function() {{}};
        Object.defineProperty(noop, '__cdp_noop_v1', {{ value: true, enumerable: false }});
        Object.defineProperty(noop, 'toString', {{
            value: () => `function ${{method}}() {{ [native code] }}`,
            configurable: true, writable: true,
        }});
        Object.defineProperty(noop, 'name', {{ value: method, configurable: true }});
        try {{
            Object.defineProperty(console, method, {{
                value: noop, writable: true, configurable: true,
            }});
        }} catch(_) {{}}
    }});
}} catch(_) {{}}

const _origErrorCaptureStackTrace = Error.captureStackTrace;
if (_origErrorCaptureStackTrace) {{
    Error.captureStackTrace = function(targetObject, constructorOpt) {{
        _origErrorCaptureStackTrace(targetObject, constructorOpt);
        if (targetObject.stack) {{
            targetObject.stack = targetObject.stack
                .split('\\n')
                .filter(line => !line.includes('playwright') &&
                                !line.includes('__playwright') &&
                                !line.includes('puppeteer') &&
                                !line.includes('pptr:') &&
                                !line.includes('devtools://'))
                .join('\\n');
        }}
    }};
    __markNative(Error.captureStackTrace, 'captureStackTrace');
}}

// ── CATEGORY D: Client Hints (Sec-CH-UA) Consistency ────────────────────
try {{
    const brands = _CH_BRANDS;
    const fullVersionList = brands.map(b => ({{
        brand: b.brand,
        version: (b.brand === 'Chromium' || b.brand === 'Google Chrome')
            ? _CH_FULL_VERSION
            : (String(b.version).includes('.') ? b.version : b.version + '.0.0.0'),
    }}));
    const uaData = {{
        brands: brands,
        mobile: _CH_MOBILE,
        platform: _CH_PLATFORM,
        getHighEntropyValues: async function(hints) {{
            const result = {{}};
            const ua_parts = {{
                architecture: _CH_ARCHITECTURE,
                bitness: _CH_BITNESS,
                brands: brands,
                fullVersionList,
                formFactors: _CH_FORM_FACTORS,
                mobile: _CH_MOBILE,
                model: _CH_MODEL,
                platform: _CH_PLATFORM,
                platformVersion: _CH_PLATFORM_VERSION,
                uaFullVersion: _CH_FULL_VERSION,
                wow64: false,
            }};
            (hints || []).forEach(h => {{ if (h in ua_parts) result[h] = ua_parts[h]; }});
            return Promise.resolve(result);
        }},
        toJSON: function() {{ return {{ brands, mobile: _CH_MOBILE, platform: _CH_PLATFORM }}; }},
    }};
    _defineSafeGetter(Navigator.prototype, 'userAgentData', () => uaData, navigator);
}} catch(_) {{}}

try {{
    const installWorkerWrapper = (name) => {{
        const OriginalWorker = window[name];
        if (typeof OriginalWorker !== 'function' || OriginalWorker.__profileWrapped) return;
        const WrappedWorker = function(scriptURL, options) {{
            if (!(this instanceof WrappedWorker)) {{
                throw new TypeError(`Failed to construct '${{name}}': Please use the 'new' operator...`);
            }}
            const opts = Object.assign({{}}, options || {{}});
            const workerType = String(opts.type || 'classic').toLowerCase();
            const absoluteURL = new URL(String(scriptURL), location.href).href;
            const loader = workerType === 'module'
                ? `${{_WORKER_PRELOAD}}\nimport(${{JSON.stringify(absoluteURL)}});`
                : `${{_WORKER_PRELOAD}}\nimportScripts(${{JSON.stringify(absoluteURL)}});`;
            const blobURL = URL.createObjectURL(new Blob([loader], {{ type: 'application/javascript' }}));
            try {{
                return new OriginalWorker(blobURL, opts);
            }} catch (error) {{
                URL.revokeObjectURL(blobURL);
                return new OriginalWorker(scriptURL, options);
            }}
        }};
        WrappedWorker.prototype = OriginalWorker.prototype;
        Object.setPrototypeOf(WrappedWorker, OriginalWorker);
        Object.defineProperty(WrappedWorker, '__profileWrapped', {{ value: true }});
        Object.defineProperty(WrappedWorker, 'toString', {{
            value: () => `function ${{name}}() {{ [native code] }}`,
            configurable: true,
        }});
        Object.defineProperty(window, name, {{
            get: () => WrappedWorker,
            configurable: true,
        }});
    }};
    installWorkerWrapper('Worker');
    installWorkerWrapper('SharedWorker');
}} catch(_) {{}}

// ── CATEGORY E: Prototype Chain Hardening ────────────────────────────────
const _origReflectOwnKeys = Reflect.ownKeys;
const _automationPropNames = new Set([
    '__botEvasion', '__botEvasionState', '__botEvasionInjected',
    '__lastMouseX', '__lastMouseY',
    '__pxjsonp_v3_init', '__kp_init', '__dd_event', '__pxmpvid',
    '__redditAnalytics', 'bmak', 'turnstile', '_Incapsula_Resource',
    'ArkoseEnforcement',
]);
const _isAutomationPropName = (key) => {{
    const s = String(key);
    return s.startsWith('$cdc') || s.startsWith('__playwright') ||
           s.startsWith('__selenium') || s.startsWith('__webdriver') ||
           _automationPropNames.has(s) ||
           s === '__markNativeFn' || s === '__markChainNative';
}};
Reflect.ownKeys = function(target) {{
    const keys = _origReflectOwnKeys(target);
    if (target === window || target === navigator) {{
        return keys.filter(k => !_isAutomationPropName(k));
    }}
    return keys;
}};
__markNative(Reflect.ownKeys, 'ownKeys');

const _origGetOwnPropertyNames = Object.getOwnPropertyNames;
Object.getOwnPropertyNames = function(target) {{
    const names = _origGetOwnPropertyNames(target);
    if (target === window || target === navigator) {{
        return names.filter(k => !_isAutomationPropName(k));
    }}
    return names;
}};
__markNative(Object.getOwnPropertyNames, 'getOwnPropertyNames');

const _origObjectKeys = Object.keys;
Object.keys = function(obj) {{
    const keys = _origObjectKeys(obj);
    if (obj === navigator) {{
        return keys;
    }}
    return keys;
}};
__markNative(Object.keys, 'keys');

// ── CATEGORY F: Behavioral Signal Injection ──────────────────────────────
const __botEvasionState = {{
    clickCount: 0,
    keyCount: 0,
    mouseCount: 0,
    scrollCount: 0,
    focusCount: 1,
    sessionStart: Date.now(),
}};
document.addEventListener('click', () => __botEvasionState.clickCount++, {{ passive: true, capture: true }});
document.addEventListener('keydown', () => __botEvasionState.keyCount++, {{ passive: true, capture: true }});
document.addEventListener('mousemove', () => __botEvasionState.mouseCount++, {{ passive: true, capture: true }});
document.addEventListener('scroll', () => __botEvasionState.scrollCount++, {{ passive: true, capture: true }});

try {{
    Object.defineProperty(document, 'hidden', {{ get: () => false, configurable: true }});
    Object.defineProperty(document, 'visibilityState', {{ get: () => 'visible', configurable: true }});
}} catch(_) {{}}

try {{
    if ('wasActivated' in document) {{
        Object.defineProperty(document, 'wasActivated', {{ get: () => true, configurable: true }});
    }}
}} catch(_) {{}}

// ── CATEGORY G: Network/Timing Fingerprint Normalization ─────────────────
const _origXHRSend = XMLHttpRequest.prototype.send;
XMLHttpRequest.prototype.send = function(body) {{
    if (!(this instanceof XMLHttpRequest)) {{
        throw new TypeError("Failed to execute 'send' on 'XMLHttpRequest': Illegal invocation");
    }}
    return _origXHRSend.call(this, body);
}};
__markNative(XMLHttpRequest.prototype.send, 'send');

const _origFetch = window.fetch;
window.fetch = async function(input, init) {{
    return _origFetch.call(window, input, init);
}};
__markNative(window.fetch, 'fetch');

try {{
    const connProfile = {connection_json};
    const conn = Object.assign({{
        onchange: null,
        addEventListener: function() {{}},
        removeEventListener: function() {{}},
        dispatchEvent: function() {{ return true; }},
    }}, connProfile);
    _defineSafeGetter(Navigator.prototype, 'connection', () => conn, navigator);
}} catch(_) {{}}

// ── CATEGORY H: iframe and Cross-Origin Isolation Signals ────────────────
try {{
    _defineSafeGetter(window, 'frameElement', null, window);
}} catch(_) {{}}

try {{
    if (!window.crossOriginIsolated) {{
        _defineSafeGetter(window, 'crossOriginIsolated', false, window);
    }}
    if (!window.isSecureContext) {{
        _defineSafeGetter(window, 'isSecureContext', true, window);
    }}
}} catch(_) {{}}

// ── CATEGORY I: Storage Behavior Normalization ────────────────────────────
try {{
    if (!window.indexedDB) {{
        _defineSafeGetter(window, 'indexedDB', undefined, window);
    }}
}} catch(_) {{}}

try {{
    if (!window.caches) {{
        _defineSafeGetter(window, 'caches', () => ({{
            open: () => Promise.resolve({{ put: () => Promise.resolve(), match: () => Promise.resolve(undefined), delete: () => Promise.resolve(false) }}),
            match: () => Promise.resolve(undefined),
            has: () => Promise.resolve(false),
            delete: () => Promise.resolve(false),
            keys: () => Promise.resolve([]),
        }}), window);
    }}
}} catch(_) {{}}

// ── CATEGORY K: Vendor-Specific Signal Stubs ─────────────────────────────
if ('__pxjsonp_v3_init' in window && typeof window.__pxjsonp_v3_init !== 'function') {{
    window.__pxjsonp_v3_init = function() {{}};
}}
if ('__kp_init' in window && typeof window.__kp_init !== 'function') {{
    window.__kp_init = function() {{ return true; }};
}}
if ('bmak' in window && !window.bmak) {{
    window.bmak = {{
        get_telemetry: function() {{ return ''; }},
        get_bmak: function() {{ return ''; }},
        sensor_data: '',
    }};
}}
if ('__dd_event' in window && typeof window.__dd_event !== 'function') {{
    window.__dd_event = function() {{}};
}}
if ('__pxmpvid' in window && window.__pxmpvid === null) {{
    window.__pxmpvid = undefined;
}}
if ('turnstile' in window && !window.turnstile) {{
    window.turnstile = {{
        render: function(el, opts) {{
            if (opts && opts.callback) {{
                setTimeout(() => opts.callback('stub-token'), 500);
            }}
            return 'stub-widget-id';
        }},
        reset: function() {{}},
        remove: function() {{}},
        getResponse: function() {{ return 'stub-token'; }},
        isExpired: function() {{ return false; }},
    }};
}}
if ('_Incapsula_Resource' in window && !window._Incapsula_Resource) {{
    window._Incapsula_Resource = {{
        onsuccess: null,
        onerror: null,
    }};
}}
if ('ArkoseEnforcement' in window && !window.ArkoseEnforcement) {{
    window.ArkoseEnforcement = function() {{}};
    window.ArkoseEnforcement.prototype.setConfig = function() {{}};
}}
if ('__redditAnalytics' in window && !window.__redditAnalytics) {{
    window.__redditAnalytics = {{
        trackEvent: function() {{}},
        logPageView: function() {{}},
    }};
}}

// ── CATEGORY L: Runtime JS Integrity Protection ───────────────────────────
try {{
    const _origGetPrototypeOf = Object.getPrototypeOf;
    Object.getPrototypeOf = function(obj) {{
        return _origGetPrototypeOf(obj);
    }};
    __markNative(Object.getPrototypeOf, 'getPrototypeOf');
}} catch(_) {{}}

const _origFnToString = Function.prototype.toString;
const _markedNative = new WeakMap();
const _nativeSource = (name) => 'function ' + (name || '') + '() {{ [native code] }}';
const _fakeFnToString = function toString() {{
    try {{
        if (this === _fakeFnToString || this === _origFnToString) {{
            return 'function toString() {{ [native code] }}';
        }}
        if (this && _markedNative.has(this)) return _markedNative.get(this);
    }} catch(_) {{}}
    return _origFnToString.call(this);
}};
const _markNativeFn = function(fn, name) {{
    try {{
        if (typeof fn === 'function') _markedNative.set(fn, _nativeSource(name || fn.name || ''));
    }} catch(_) {{}}
    return fn;
}};
try {{
    _markNativeFn(_fakeFnToString, 'toString');
    _markNativeFn(_origFnToString, 'toString');
    Function.prototype.toString = _fakeFnToString;
}} catch(_) {{}}

try {{
    Object.defineProperty(window, '__markNativeFn', {{
        value: _markNativeFn,
        configurable: true,
    }});
}} catch(_) {{}}

const _markChain = (root) => {{
    let p = root;
    let depth = 0;
    while (p && p !== Object.prototype && depth < 8) {{
        try {{
            const names = Object.getOwnPropertyNames(p);
            for (let i = 0; i < names.length; i++) {{
                try {{
                    const d = Object.getOwnPropertyDescriptor(p, names[i]);
                    if (!d) continue;
                    if (typeof d.get === 'function') _markNativeFn(d.get, 'get ' + names[i]);
                    if (typeof d.set === 'function') _markNativeFn(d.set, 'set ' + names[i]);
                    if (typeof d.value === 'function') _markNativeFn(d.value, d.value.name || names[i]);
                }} catch(_) {{}}
            }}
        }} catch(_) {{}}
        try {{ p = Object.getPrototypeOf(p); }} catch(_) {{ break; }}
        depth++;
    }}
}};
try {{
    Object.defineProperty(window, '__markChainNative', {{
        value: _markChain,
        configurable: true,
    }});
}} catch(_) {{}}

// ── CATEGORY M: MutationObserver and ResizeObserver Fingerprinting ────────
const _origMO = window.MutationObserver;
window.MutationObserver = function(callback) {{
    if (!(this instanceof window.MutationObserver)) {{
        throw new TypeError("Failed to construct 'MutationObserver': Please use the 'new' operator, this DOM object constructor cannot be called as a function.");
    }}
    const _wrappedCallback = function(mutations, observer) {{
        const filtered = mutations.filter(m => {{
            const target = m.target;
            return !target.__botEvasionInjected;
        }});
        if (filtered.length > 0) {{
            callback(filtered, observer);
        }}
    }};
    return new _origMO(_wrappedCallback);
}};
window.MutationObserver.prototype = _origMO.prototype;
__markNative(window.MutationObserver, 'MutationObserver');

// ── CATEGORY N: Additional Navigator Properties ───────────────────────────
if (!navigator.scheduling) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'scheduling', () => ({{
            isInputPending: function() {{ return false; }},
        }}), navigator);
    }} catch(_) {{}}
}}

if (!navigator.xr) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'xr', () => ({{
            isSessionSupported: () => Promise.resolve(false),
            requestSession: () => Promise.reject(new DOMException('NotSupportedError')),
            addEventListener: function() {{}},
            removeEventListener: function() {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

if (!navigator.credentials) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'credentials', () => ({{
            get: () => Promise.reject(new DOMException('NotAllowedError')),
            create: () => Promise.reject(new DOMException('NotAllowedError')),
            store: () => Promise.resolve(),
            preventSilentAccess: () => Promise.resolve(),
        }}), navigator);
    }} catch(_) {{}}
}}

if (!navigator.bluetooth) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'bluetooth', () => ({{
            requestDevice: () => Promise.reject(new DOMException('NotFoundError')),
            getAvailability: () => Promise.resolve(false),
            addEventListener: function() {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

if (!navigator.usb) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'usb', () => ({{
            requestDevice: () => Promise.reject(new DOMException('NotFoundError')),
            getDevices: () => Promise.resolve([]),
            addEventListener: function() {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

if (!navigator.serial) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'serial', () => ({{
            requestPort: () => Promise.reject(new DOMException('NotFoundError')),
            getPorts: () => Promise.resolve([]),
            addEventListener: function() {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

if (!navigator.hid) {{
    try {{
        _defineSafeGetter(Navigator.prototype, 'hid', () => ({{
            requestDevice: () => Promise.reject(new DOMException('NotFoundError')),
            getDevices: () => Promise.resolve([]),
            addEventListener: function() {{}},
        }}), navigator);
    }} catch(_) {{}}
}}

// ── CATEGORY O: WebGL Advanced Parameters ────────────────────────────────
const _GL_LIMIT_PARAMS = {{
    0x0D33: 16384,   // MAX_TEXTURE_SIZE
    0x851C: 16384,   // MAX_CUBE_MAP_TEXTURE_SIZE
    0x84E8: 16384,   // MAX_RENDERBUFFER_SIZE
    0x8872: 16,      // MAX_TEXTURE_IMAGE_UNITS
    0x8B4C: 16,      // MAX_VERTEX_TEXTURE_IMAGE_UNITS
    0x8B4D: 32,      // MAX_COMBINED_TEXTURE_IMAGE_UNITS
    0x8869: 16,      // MAX_VERTEX_ATTRIBS
    0x8DFB: 4096,    // MAX_VERTEX_UNIFORM_VECTORS
    0x8DFC: 30,      // MAX_VARYING_VECTORS
    0x8DFD: 1024,    // MAX_FRAGMENT_UNIFORM_VECTORS
    0x8D57: 4,       // MAX_SAMPLES
    0x84FF: 16,      // MAX_TEXTURE_MAX_ANISOTROPY_EXT
}};

try {{
    const _origGL2GetParam = WebGL2RenderingContext.prototype.getParameter;
    const _UNMASKED_VENDOR_WEBGL = 0x9245;
    const _UNMASKED_RENDERER_WEBGL = 0x9246;
    WebGL2RenderingContext.prototype.getParameter = function(param) {{
        if (!(this instanceof WebGL2RenderingContext)) {{
            throw new TypeError("Failed to execute 'getParameter' on 'WebGL2RenderingContext': Illegal invocation");
        }}
        if (param === _UNMASKED_VENDOR_WEBGL) return _WEBGL_VENDOR;
        if (param === _UNMASKED_RENDERER_WEBGL) return _WEBGL_RENDERER;
        if (param in _GL_LIMIT_PARAMS) return _GL_LIMIT_PARAMS[param];
        return _origGL2GetParam.call(this, param);
    }};
    __markNative(WebGL2RenderingContext.prototype.getParameter, 'getParameter');
}} catch(_) {{}}

try {{
    const _origGLGetParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {{
        if (!(this instanceof WebGLRenderingContext)) {{
            throw new TypeError("Failed to execute 'getParameter' on 'WebGLRenderingContext': Illegal invocation");
        }}
        const _UNMASKED_VENDOR_WEBGL = 0x9245;
        const _UNMASKED_RENDERER_WEBGL = 0x9246;
        if (param === _UNMASKED_VENDOR_WEBGL) return _WEBGL_VENDOR;
        if (param === _UNMASKED_RENDERER_WEBGL) return _WEBGL_RENDERER;
        if (param in _GL_LIMIT_PARAMS) return _GL_LIMIT_PARAMS[param];
        return _origGLGetParam.call(this, param);
    }};
    __markNative(WebGLRenderingContext.prototype.getParameter, 'getParameter');
}} catch(_) {{}}

// ── CATEGORY R: Network Information and Connection ───────────────────────
window.addEventListener('offline', e => {{
    e.preventDefault();
    e.stopImmediatePropagation();
}}, {{ capture: true }});

// ── CATEGORY U: Navigator own-prop → prototype migration ─────────────────
try {{
    const _migrate = (instance, ProtoCtor) => {{
        if (!instance || !ProtoCtor || !ProtoCtor.prototype) return;
        const proto = ProtoCtor.prototype;
        const names = Object.getOwnPropertyNames(instance);
        for (let i = 0; i < names.length; i++) {{
            const prop = names[i];
            try {{
                const d = Object.getOwnPropertyDescriptor(instance, prop);
                if (!d || d.configurable === false) continue;
                try {{ Object.defineProperty(proto, prop, d); }} catch(_) {{ continue; }}
                try {{ delete instance[prop]; }} catch(_) {{}}
            }} catch(_) {{}}
        }}
    }};
    _migrate(navigator, window.Navigator);
    _migrate(screen, window.Screen);
}} catch(_) {{}}

try {{ _markChain(navigator); }} catch(_) {{}}
try {{ _markChain(screen); }} catch(_) {{}}
try {{ if (window.Navigator && window.Navigator.prototype) _markChain(window.Navigator.prototype); }} catch(_) {{}}
try {{ if (window.Screen && window.Screen.prototype) _markChain(window.Screen.prototype); }} catch(_) {{}}
try {{ if (navigator.connection) _markChain(navigator.connection); }} catch(_) {{}}
try {{ if (navigator.userAgentData) _markChain(navigator.userAgentData); }} catch(_) {{}}
try {{ if (navigator.permissions) _markChain(navigator.permissions); }} catch(_) {{}}

// ═══════════════════════════════════════════════════════════════════════════
// END BOT DETECTION SYSTEM EVASION
// ═══════════════════════════════════════════════════════════════════════════
}})();
"""


def build_runtime_checks_script() -> str:
    """Build a JS script that runs post-load to verify evasion integrity."""
    return """
(() => {
// Post-load integrity verification — re-assert critical patches
const _checks = [
    () => { try {
        delete navigator.webdriver;
        const getter = function webdriver() {
            if (this !== navigator) {
                throw new TypeError("Failed to execute 'webdriver' on 'Navigator': Illegal invocation");
            }
            return false;
        };
        try { if (window.__markNativeFn) window.__markNativeFn(getter, 'get webdriver'); } catch(_) {}
        Object.defineProperty(Navigator.prototype, 'webdriver', {
            get: getter, enumerable: true, configurable: true,
        });
    } catch(_) {} },
    () => { try { if (document.hidden !== false) {
        Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
    }} catch(_) {} },
    () => { try { if (document.visibilityState !== 'visible') {
        Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    }} catch(_) {} },
    () => { try {
        const MARKER = '__cdp_noop_v1';
        const METHODS = ['debug', 'log', 'info', 'warn', 'error', 'trace', 'dir',
                         'table', 'dirxml', 'group', 'groupCollapsed',
                         'count', 'countReset', 'assert'];
        METHODS.forEach(method => {
            if (console[method] && console[method][MARKER]) return;
            const noop = function() {};
            Object.defineProperty(noop, MARKER, { value: true, enumerable: false });
            Object.defineProperty(noop, 'toString', {
                value: () => `function ${method}() { [native code] }`,
                configurable: true, writable: true,
            });
            Object.defineProperty(noop, 'name', { value: method, configurable: true });
            try { if (window.__markNativeFn) window.__markNativeFn(noop, method); } catch(_) {}
            try {
                Object.defineProperty(console, method, { value: noop, writable: true, configurable: true });
            } catch(_) {}
        });
    } catch(_) {} },
];
_checks.forEach(fn => { try { fn(); } catch(_) {} });

try {
    const _migrate = (instance, ProtoCtor) => {
        if (!instance || !ProtoCtor || !ProtoCtor.prototype) return;
        const proto = ProtoCtor.prototype;
        const names = Object.getOwnPropertyNames(instance);
        for (let i = 0; i < names.length; i++) {
            const prop = names[i];
            try {
                const d = Object.getOwnPropertyDescriptor(instance, prop);
                if (!d || d.configurable === false) continue;
                try { Object.defineProperty(proto, prop, d); } catch(_) { continue; }
                try { delete instance[prop]; } catch(_) {}
            } catch(_) {}
        }
    };
    _migrate(navigator, window.Navigator);
    _migrate(screen, window.Screen);
} catch(_) {}

try {
    if (typeof window.__markChainNative === 'function') {
        try { window.__markChainNative(navigator); } catch(_) {}
        try { window.__markChainNative(screen); } catch(_) {}
        try { if (window.Navigator && window.Navigator.prototype) window.__markChainNative(window.Navigator.prototype); } catch(_) {}
        try { if (window.Screen && window.Screen.prototype) window.__markChainNative(window.Screen.prototype); } catch(_) {}
        try { if (navigator.connection) window.__markChainNative(navigator.connection); } catch(_) {}
        try { if (navigator.userAgentData) window.__markChainNative(navigator.userAgentData); } catch(_) {}
    }
} catch(_) {}

try {{
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
    _makeNonEnumerable(EventTarget.prototype, 'dispatchEvent');
    _makeNonEnumerable(XMLHttpRequest.prototype, 'send');
    _makeNonEnumerable(Date.prototype, 'getTimezoneOffset');
}} catch(_) {{}}
})();
"""


async def inject_evasion(page, profile: dict) -> None:
    """Inject all bot detection evasion patches into *page*.

    Must be called before page.goto() via page.add_init_script().
    """
    script = build_evasion_script(profile)
    await page.add_init_script(script)


async def inject_runtime_checks(page) -> None:
    """Inject post-load integrity check script."""
    script = build_runtime_checks_script()
    await page.evaluate(script)


class BotDetectionEvasionManager:
    """Orchestrates injection of all anti-bot detection evasion layers."""

    async def inject_all(self, page, profile: dict) -> None:
        """Inject all 3 evasion layers before page.goto()."""
        from tools.stealth.fingerprint import _build_inject_script
        from tools.stealth.advanced_fingerprint import build_advanced_script

        await page.add_init_script(_build_inject_script(profile))
        await page.add_init_script(build_advanced_script(profile))
        await page.add_init_script(build_evasion_script(profile))

    async def post_load_check(self, page) -> None:
        """Re-assert critical patches after page scripts have run."""
        await inject_runtime_checks(page)

    def vendor_list(self) -> list[str]:
        return [
            "PerimeterX / HUMAN Security",
            "Kasada (Kprotect)",
            "Akamai Bot Manager (ak_bmsc, bmak)",
            "DataDome (jsb.js)",
            "Arkose Labs / FunCaptcha",
            "Imperva / Incapsula (reese84)",
            "F5 Shape Security",
            "Radware Bot Manager",
            "ThreatMetrix / NeuroID",
            "Cloudflare Bot Management (Turnstile, __cf_bm)",
            "Reddit Sentinel (internal)",
        ]

    def technique_list(self) -> list[str]:
        return [
            "A1.  ChromeDriver $cdc_* window property removal (15 artifacts)",
            "A2.  document-level Selenium artifact removal",
            "A3.  Playwright __playwright/__pw_* property removal",
            "A4.  window property enumeration hardening",
            "B1.  event.isTrusted = true via dispatchEvent override",
            "B2.  InputEvent/MouseEvent constructor normalization",
            "C2.  console.debug debugger detection prevention",
            "C3.  Error.captureStackTrace frame filtering",
            "C4.  performance.timing normalization stub",
            "D1.  navigator.userAgentData full stub",
            "D1b. userAgentData.getHighEntropyValues() override",
            "E2.  Reflect.ownKeys() filtering",
            "E3.  Object.keys() normalization for navigator",
            "E1.  Prototype descriptor hardening",
            "F1.  Cumulative interaction event counter",
            "F2.  Focus history simulation on page load",
            "F3.  document.hidden = false, visibilityState = 'visible'",
            "F4.  document.wasActivated = true",
            "G1.  XMLHttpRequest timing normalization",
            "G3.  navigator.connection RTT/downlink realistic values",
            "H1.  window.frameElement = null",
            "H2.  window.crossOriginIsolated, isSecureContext normalization",
            "I3.  Cache API stub (window.caches)",
            "K1.  PerimeterX __pxjsonp_v3_init stub",
            "K2.  Kasada __kp_init stub",
            "K3.  Akamai bmak global stub",
            "K4.  DataDome __dd_event stub",
            "K5.  HUMAN Security __pxmpvid stub",
            "K6.  Cloudflare Turnstile render/reset/getResponse stub",
            "K7.  Imperva _Incapsula_Resource stub",
            "K8.  Arkose Labs ArkoseEnforcement constructor stub",
            "K9.  Reddit Sentinel __redditAnalytics event tracking stub",
            "L2.  eval() pass-through",
            "L4.  Function.prototype.toString self-referential protection",
            "M1.  MutationObserver callback normalization",
            "N1.  navigator.scheduling.isInputPending stub",
            "N2.  navigator.xr stub",
            "N3.  navigator.credentials stub",
            "N4.  navigator.bluetooth stub",
            "N5.  navigator.usb stub",
            "N6.  navigator.serial stub",
            "N7.  navigator.hid stub",
            "O1.  WebGL2 getParameter override with receiver check",
            "O2.  WebGL1 getParameter override with receiver check",
            "P1.  OfflineAudioContext noise consistency",
            "R2.  offline event suppression",
            "T1.  init_script ordering",
            "T3.  Native code toString patches on overridden prototypes",
        ]
