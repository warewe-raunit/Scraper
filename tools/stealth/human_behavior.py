"""
tools/stealth/human_behavior.py — HumanBehaviorEngine

Multi-layered randomization system producing timing distributions
indistinguishable from real human behavior.

Architecture:
- Layer 1: Log-normal distribution (models human reaction times)
- Layer 2: Session fatigue factor (increases over time)
- Layer 3: Distraction spikes (occasional long pauses)
- Layer 4: Time-of-day modulation (circadian rhythm)
- Layer 5: Micro-jitter from Perlin-like noise (prevents repetition)
"""

import asyncio
import copy
import hashlib
import math
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

# Scale all delays globally. Set STEALTH_DELAY_SCALE=0.1 in .env for fast dev runs.
_DELAY_SCALE = float(os.getenv("STEALTH_DELAY_SCALE", "1.0"))


@dataclass
class BehaviorPersonality:
    """Deterministic behavior profile generated from account_id hash."""
    base_speed: float        # 0.7 = fast typist, 1.3 = slow typist
    distraction_rate: float  # 0.03 = focused, 0.12 = easily distracted
    scroll_style: str        # "skimmer" | "reader" | "scanner"
    typing_style: str        # "hunt_peck" | "touch_typist" | "mobile_thumbs"
    session_stamina: float   # 0.5 = tires quickly, 1.5 = marathon sessions
    pause_between_words: float
    reading_speed_wpm: int   # 150-400 range


@dataclass
class SessionState:
    """Tracks current session state for fatigue and momentum modeling."""
    session_start_ts: float = field(default_factory=time.time)
    actions_this_session: int = 0
    last_action_ts: float = field(default_factory=time.time)
    current_fatigue: float = 0.0
    current_engagement: float = 0.5
    consecutive_same_action: int = 0


class HumanRNG:
    """Multi-layered RNG for human-like timing."""

    def __init__(self, personality: BehaviorPersonality, timezone_offset: int = 0, config: Optional[dict] = None):
        self.personality = personality
        self.timezone_offset = timezone_offset
        self.config = config or {}
        self._noise_offset = random.random() * 1000

    def _box_muller_normal(self) -> float:
        u1 = random.random()
        u2 = random.random()
        if u1 < 1e-10:
            u1 = 1e-10
        return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)

    def _log_normal(self, median: float, sigma: float) -> float:
        if median <= 0:
            median = 0.001
        mu = math.log(median)
        z = self._box_muller_normal()
        return math.exp(mu + sigma * z)

    def _perlin_noise_1d(self, t: float) -> float:
        i0 = int(t)
        i1 = i0 + 1
        f = t - i0
        f = f * f * (3 - 2 * f)
        g0 = math.sin(i0 * 12.9898 + self._noise_offset) * 43758.5453
        g0 = g0 - int(g0)
        g0 = (g0 * 2) - 1
        g1 = math.sin(i1 * 12.9898 + self._noise_offset) * 43758.5453
        g1 = g1 - int(g1)
        g1 = (g1 * 2) - 1
        return g0 * (1 - f) + g1 * f

    def _circadian_factor(self) -> float:
        utc_hour = (time.time() / 3600) % 24
        local_hour = (utc_hour + self.timezone_offset) % 24
        factor = 0.7 + 0.3 * math.sin((local_hour - 6) * math.pi / 12)
        return max(0.4, min(1.0, factor))

    def generate(self, median: float, sigma: float, fatigue: float = 0.0,
                 distraction_chance: float = 0.0, min_val: float = 0.0,
                 max_val: float = float('inf'), context: str = "") -> float:
        value = self._log_normal(median, sigma)
        fatigue_multiplier = 1.0 + (fatigue * 0.5 * (2.0 - self.personality.session_stamina))
        value *= fatigue_multiplier
        distraction_rate = self.personality.distraction_rate
        if random.random() < (distraction_chance or distraction_rate):
            value *= random.uniform(3.0, 8.0)
        circadian = self._circadian_factor()
        speed_factor = circadian / self.personality.base_speed
        value /= speed_factor
        noise_t = time.time() + hash(context) % 1000
        noise_val = self._perlin_noise_1d(noise_t / 10.0)
        jitter_amplitude = self.config.get('jitter_amplitude', 0.05)
        value *= (1.0 + noise_val * jitter_amplitude)
        return max(min_val, min(max_val, value))


class HumanBehaviorEngine:
    """Main behavior engine providing human-like timing for bot actions."""

    DELAY_PROFILES = {
        "pre_click": {"median": 0.25, "sigma": 0.25, "min": 0.08, "max": 0.6},
        "post_submit": {"median": 6.0, "sigma": 0.5, "min": 2.0, "max": 20.0},
        "between_pages": {"median": 12.0, "sigma": 0.6, "min": 3.0, "max": 45.0},
        "reading": {"median": 30.0, "sigma": 0.5, "min": 5.0, "max": 120.0},
        "thinking_before_reply": {"median": 25.0, "sigma": 0.7, "min": 8.0, "max": 90.0},
    }

    FAST_BIGRAMS = {
        'th', 'he', 'in', 'er', 'an', 're', 'on', 'at', 'en', 'nd',
        'ti', 'es', 'or', 'te', 'of', 'ed', 'is', 'it', 'al', 'ar',
        'st', 'to', 'nt', 'ng', 'se', 'ha', 'as', 'ou', 'io', 'le',
        've', 'co', 'me', 'de', 'hi', 'ri', 'ro', 'ic', 'ne', 'ea'
    }

    SLOW_CHARS = set('0123456789!@#$%^&*()_+-=[]{}|;\':",./<>?`~')

    def __init__(self, account_id: str, timezone: int = 0, config_overrides: Optional[dict] = None):
        self.account_id = account_id
        self.timezone = timezone
        self.config = {'jitter_amplitude': 0.05, 'typo_base_rate': 0.03, 'engagement_fluctuation': 0.2}
        if config_overrides:
            self.config.update(config_overrides)
        self.personality = self._generate_personality(account_id)
        self.rng = HumanRNG(self.personality, timezone, self.config)
        self.session = SessionState()
        # deepcopy so each instance gets its own dict — prevents class-level mutation
        self.DELAY_PROFILES = copy.deepcopy(HumanBehaviorEngine.DELAY_PROFILES)
        self._adjust_profiles_for_personality()

    def _generate_personality(self, account_id: str) -> BehaviorPersonality:
        hash_bytes = hashlib.sha256(f"{account_id}personality".encode('utf-8')).digest()

        def byte_to_float(b: int, min_val: float, max_val: float) -> float:
            return min_val + (b / 255.0) * (max_val - min_val)

        return BehaviorPersonality(
            base_speed=byte_to_float(hash_bytes[0], 0.7, 1.3),
            distraction_rate=byte_to_float(hash_bytes[1], 0.03, 0.12),
            session_stamina=byte_to_float(hash_bytes[2], 0.5, 1.5),
            pause_between_words=byte_to_float(hash_bytes[3], 0.8, 1.5),
            reading_speed_wpm=int(byte_to_float(hash_bytes[4], 150, 400)),
            scroll_style=["skimmer", "reader", "scanner"][hash_bytes[5] % 3],
            typing_style=["hunt_peck", "touch_typist", "mobile_thumbs"][hash_bytes[6] % 3],
        )

    def _adjust_profiles_for_personality(self):
        speed_mult = self.personality.base_speed * _DELAY_SCALE
        for profile in self.DELAY_PROFILES.values():
            profile['median'] *= speed_mult
            profile['min'] *= speed_mult
            profile['max'] *= speed_mult

    def _update_session(self, context: str):
        now = time.time()
        self.session.actions_this_session += 1
        time_since_last = now - self.session.last_action_ts
        fatigue_increase = 0.01 + (time_since_last / 3600) * 0.1 + 0.005
        self.session.current_fatigue = min(1.0, self.session.current_fatigue + fatigue_increase)
        engagement_change = random.uniform(-self.config['engagement_fluctuation'], self.config['engagement_fluctuation'])
        engagement_change *= (1.0 - self.personality.distraction_rate)
        self.session.current_engagement = max(0.0, min(1.0, self.session.current_engagement + engagement_change))
        self.session.consecutive_same_action += 1
        self.session.last_action_ts = now

    async def delay(self, context: str, min_s: Optional[float] = None, max_s: Optional[float] = None) -> float:
        profile = self.DELAY_PROFILES.get(context, self.DELAY_PROFILES["pre_click"])
        distraction_chance = 0.05
        if context in ["between_pages", "post_submit"]:
            distraction_chance = self.personality.distraction_rate * 2
        delay_s = self.rng.generate(
            median=profile['median'], sigma=profile['sigma'],
            fatigue=self.session.current_fatigue, distraction_chance=distraction_chance,
            min_val=min_s if min_s is not None else profile['min'],
            max_val=max_s if max_s is not None else profile['max'],
            context=context,
        )
        self._update_session(context)
        await asyncio.sleep(delay_s)
        return delay_s

    def human_scroll_count(self, context: str) -> int:
        if context == "reading_post":
            count = int(max(3, min(12, self.rng._log_normal(6, 0.3))))
        elif context == "browsing_feed":
            base = self.rng._log_normal(8, 0.5)
            count = int(max(2, min(20, base * (1.0 + self.session.current_engagement))))
        elif context == "looking_for_comment":
            count = int(max(4, min(15, self.rng._log_normal(7, 0.4) * 0.9)))
        else:
            count = int(self.rng._log_normal(5, 0.5))
        self._update_session(f"scroll_{context}")
        return count

    def human_scroll_distance(self) -> int:
        roll = random.random()
        if roll < 0.2:
            distance = random.randint(80, 200)
        elif roll < 0.7:
            distance = random.randint(200, 500)
        else:
            distance = random.randint(500, 1000)
        mult = {"skimmer": 1.5, "reader": 0.7, "scanner": 1.0}.get(self.personality.scroll_style, 1.0)
        return int(distance * mult)

    def human_type_delay(self, char: str, prev_char: str = "", word_position: int = 0, sentence_position: int = 0) -> int:
        base_delay = 100
        mult = {"hunt_peck": 2.0, "touch_typist": 0.8, "mobile_thumbs": 1.3}.get(self.personality.typing_style, 1.0)
        base_delay *= mult
        bigram = (prev_char + char).lower()
        if len(bigram) == 2 and bigram in self.FAST_BIGRAMS:
            base_delay *= random.uniform(0.3, 0.6)
        if char in self.SLOW_CHARS:
            base_delay *= random.uniform(2.0, 3.0)
        if word_position == 0 and prev_char == ' ':
            base_delay *= self.personality.pause_between_words
        if prev_char in '.?!':
            base_delay = random.randint(300, 2000)
        delay = int(base_delay * random.uniform(0.8, 1.2))
        typo_rate = self.config['typo_base_rate'] + (self.personality.distraction_rate * 0.2)
        if random.random() < typo_rate:
            delay += random.randint(200, 800)
        return max(35, min(2500, delay))

    def human_mouse_move(self, start_x: int, start_y: int, end_x: int, end_y: int, num_points: int = 20) -> list[tuple[int, int, int]]:
        waypoints = []
        mid_x = (start_x + end_x) / 2
        mid_y = (start_y + end_y) / 2
        dx = end_x - start_x
        dy = end_y - start_y
        length = math.sqrt(dx * dx + dy * dy)
        if length > 0:
            perp_x = -dy / length
            perp_y = dx / length
            curve_offset = random.uniform(-0.3, 0.3) * length
            cp1_x = mid_x + perp_x * curve_offset * 0.5
            cp1_y = mid_y + perp_y * curve_offset * 0.5
            cp2_x = mid_x + perp_x * curve_offset * 0.3
            cp2_y = mid_y + perp_y * curve_offset * 0.3
        else:
            cp1_x = cp1_y = cp2_x = cp2_y = mid_x

        for i in range(num_points + 1):
            t = i / num_points
            p0, p1, p2, p3 = (1-t)**3, 3*(1-t)**2*t, 3*(1-t)*t**2, t**3
            x = int(p0*start_x + p1*cp1_x + p2*cp2_x + p3*end_x) + random.randint(-2, 2)
            y = int(p0*start_y + p1*cp1_y + p2*cp2_y + p3*end_y) + random.randint(-2, 2)
            if t > 0.8:
                overshoot = (t - 0.8) / 0.2
                x += int(random.randint(5, 30) * overshoot)
                y += int(random.randint(5, 30) * overshoot)
            delay_mult = 1.5 if t < 0.2 or t > 0.8 else 0.8
            waypoints.append((x, y, int(16 * delay_mult + random.randint(0, 8))))

        if random.random() < 0.3:
            waypoints.append((end_x + random.randint(-10, 10), end_y + random.randint(-10, 10), random.randint(50, 150)))
            waypoints.append((end_x, end_y, random.randint(30, 80)))
        return waypoints

    def human_reading_time(self, text_length_chars: int, has_images: bool = False) -> float:
        words = text_length_chars / 5
        base_seconds = (words / self.personality.reading_speed_wpm) * 60
        if has_images:
            base_seconds += random.uniform(2, 5)
        if self.session.current_engagement > 0.7:
            base_seconds *= random.uniform(1.1, 1.3)
        elif self.session.current_engagement < 0.3:
            base_seconds *= random.uniform(0.6, 0.8)
        if self.session.current_fatigue > 0.6:
            if random.random() < 0.5:
                base_seconds *= random.uniform(0.5, 0.7)
            else:
                base_seconds += random.uniform(5, 15)
        return max(5.0, min(120.0, base_seconds))

    def should_take_break(self) -> tuple[bool, float]:
        actions = self.session.actions_this_session
        if random.random() < 0.02:
            return (True, random.uniform(300, 900))
        if 5 <= actions < 15:
            chance, duration = 0.20, random.uniform(30, 120)
        elif 15 <= actions < 30:
            chance, duration = 0.40, random.uniform(60, 300)
        elif actions >= 30:
            chance, duration = 0.70, random.uniform(120, 600)
        else:
            return (False, 0.0)
        if random.random() < chance:
            self.session.current_fatigue = max(0.0, self.session.current_fatigue - 0.5)
            self.session.actions_this_session = 0
            self.session.consecutive_same_action = 0
            return (True, duration)
        return (False, 0.0)


def create_engine(account_id: str, timezone: int = 0, config_overrides: Optional[dict] = None) -> HumanBehaviorEngine:
    """Factory function to create a HumanBehaviorEngine."""
    return HumanBehaviorEngine(account_id, timezone, config_overrides)
