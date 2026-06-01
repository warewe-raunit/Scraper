"""tools/stealth — Stealth and anti-bot evasion modules."""

from tools.stealth.fingerprint import BrowserProfileManager
from tools.stealth.human_behavior import HumanBehaviorEngine, create_engine
from tools.stealth.advanced_fingerprint import (
    AdvancedFingerprintManager,
    inject_advanced,
    build_advanced_script,
)
from tools.stealth.bot_detection_evasion import (
    BotDetectionEvasionManager,
    inject_evasion,
    build_evasion_script,
    inject_runtime_checks,
)
from tools.stealth.helpers import (
    simulate_reading,
    browse_random_posts,
    find_high_engagement_posts,
    ensure_token_captured,
    _delay,
    _random_scroll,
    _human_type,
    _human_like_mouse_move,
    _tap_or_mouse_click,
    _ghost_move_and_click,
    _resolve_editable_element,
    scroll_to_comment,
    safe_proxy_id,
    _ok,
    _fail,
    _ms,
)
from tools.stealth.captcha import (
    solve_login_recaptcha,
    inject_grecaptcha_override,
    CaptchaError,
    CaptchaProviderError,
)

__all__ = [
    "BrowserProfileManager",
    "AdvancedFingerprintManager",
    "inject_advanced",
    "build_advanced_script",
    "BotDetectionEvasionManager",
    "inject_evasion",
    "build_evasion_script",
    "inject_runtime_checks",
    "HumanBehaviorEngine",
    "create_engine",
    "simulate_reading",
    "browse_random_posts",
    "find_high_engagement_posts",
    "ensure_token_captured",
    "solve_login_recaptcha",
    "inject_grecaptcha_override",
    "CaptchaError",
    "CaptchaProviderError",
]
