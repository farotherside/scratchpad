"""
animator.py — Keyframe interpolation, idle animation, blink scheduling,
              and the runtime animation state machine.

The Animator runs in the main thread.  Call .tick(now) each frame to get
an up-to-date FaceParams.
"""

import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional

from .face_model import FaceParams, EMOTIONS


# ---------------------------------------------------------------------------
# Keyframe
# ---------------------------------------------------------------------------
@dataclass
class Keyframe:
    t: float                   # absolute time (seconds)
    viseme_index: int = 0
    viseme_weight: float = 0.0
    emotion_a: str = "neutral"
    emotion_b: str = "neutral"
    emotion_blend: float = 0.0
    head_yaw: float = 0.0
    head_pitch: float = 0.0
    head_roll: float = 0.0


def _lerp(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def _smoothstep(t: float) -> float:
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def _interp_keyframes(ka: Keyframe, kb: Keyframe, now: float) -> dict:
    """Linearly interpolate between two keyframes."""
    span = kb.t - ka.t
    if span <= 0:
        f = 1.0
    else:
        f = _smoothstep((now - ka.t) / span)

    return {
        "viseme_index":  kb.viseme_index if f > 0.5 else ka.viseme_index,
        "viseme_weight": _lerp(ka.viseme_weight, kb.viseme_weight, f),
        "emotion_a":     kb.emotion_a if f > 0.5 else ka.emotion_a,
        "emotion_b":     kb.emotion_b if f > 0.5 else ka.emotion_b,
        "emotion_blend": _lerp(ka.emotion_blend, kb.emotion_blend, f),
        "head_yaw":      _lerp(ka.head_yaw,   kb.head_yaw,   f),
        "head_pitch":    _lerp(ka.head_pitch, kb.head_pitch, f),
        "head_roll":     _lerp(ka.head_roll,  kb.head_roll,  f),
    }


# ---------------------------------------------------------------------------
# Idle animation — slow Perlin-ish noise via summed sines
# ---------------------------------------------------------------------------
class IdleMotion:
    """Generates gentle continuous head movement and breathing."""

    def __init__(self):
        # Random phase offsets so each instance is unique
        self._phases = {k: random.uniform(0, math.tau) for k in
                        ("y1", "y2", "p1", "p2", "r1")}

    def get(self, now: float) -> tuple[float, float, float]:
        """Returns (idle_yaw, idle_pitch, idle_roll) in radians."""
        p = self._phases
        yaw   = (0.015 * math.sin(now * 0.11 + p["y1"]) +
                 0.008 * math.sin(now * 0.31 + p["y2"]))
        pitch = (0.010 * math.sin(now * 0.13 + p["p1"]) +
                 0.005 * math.sin(now * 0.27 + p["p2"]))
        roll  =  0.006 * math.sin(now * 0.09 + p["r1"])
        return yaw, pitch, roll


# ---------------------------------------------------------------------------
# Blink scheduler
# ---------------------------------------------------------------------------
class BlinkScheduler:
    """Schedules spontaneous blinks and drives the blink blend weight."""

    BLINK_DURATION = 0.14    # seconds for a full blink
    BLINK_INTERVAL_MIN = 2.5
    BLINK_INTERVAL_MAX = 7.0

    def __init__(self):
        self._next_blink = time.monotonic() + random.uniform(
            self.BLINK_INTERVAL_MIN, self.BLINK_INTERVAL_MAX)
        self._blink_start: Optional[float] = None

    def get_weight(self, now: float) -> float:
        if self._blink_start is None and now >= self._next_blink:
            self._blink_start = now
            self._next_blink = now + random.uniform(
                self.BLINK_INTERVAL_MIN, self.BLINK_INTERVAL_MAX)

        if self._blink_start is not None:
            elapsed = now - self._blink_start
            half = self.BLINK_DURATION / 2
            if elapsed < half:
                w = elapsed / half          # open → closed
            elif elapsed < self.BLINK_DURATION:
                w = 1.0 - (elapsed - half) / half  # closed → open
            else:
                w = 0.0
                self._blink_start = None
            return _smoothstep(w)

        return 0.0

    def force_blink(self, now: float):
        self._blink_start = now


# ---------------------------------------------------------------------------
# Main Animator
# ---------------------------------------------------------------------------
class Animator:
    """
    Maintains a sorted keyframe timeline for lipsync and expression,
    plus continuous idle motion and blink scheduling.

    Thread safety: keyframes are appended from the audio thread;
    tick() is called from the render thread.  The GIL makes simple
    list.append + slice operations safe enough here without a Lock.
    """

    def __init__(self, emotion: str = "neutral"):
        self._emotion = emotion if emotion in EMOTIONS else "neutral"
        self._keyframes: list[Keyframe] = []
        self._idle = IdleMotion()
        self._blinker = BlinkScheduler()
        # Current interpolated state (updated by tick)
        self._current: Optional[Keyframe] = None

    # --- Public API ---

    def set_emotion(self, emotion: str, blend_time: float = 0.5):
        """Smoothly transition to a new emotion."""
        if emotion not in EMOTIONS:
            return
        now = time.monotonic()
        # Insert a keyframe that blends from current to new
        kf = Keyframe(
            t=now + blend_time,
            emotion_a=self._emotion,
            emotion_b=emotion,
            emotion_blend=1.0,
        )
        self._keyframes.append(kf)
        self._emotion = emotion

    def load_lipsync(self, keyframes: list[Keyframe]):
        """Replace the upcoming keyframe timeline with a lipsync sequence."""
        now = time.monotonic()
        # Keep any keyframes that are in the past (emotion transitions)
        past = [kf for kf in self._keyframes if kf.t < now]
        future = sorted(keyframes, key=lambda k: k.t)
        self._keyframes = past + future

    def tick(self, now: Optional[float] = None) -> FaceParams:
        """Advance the animator to *now* and return a FaceParams."""
        if now is None:
            now = time.monotonic()

        params = FaceParams()
        params.emotion_a = self._emotion
        params.emotion_b = self._emotion
        params.emotion_blend = 0.0

        # Find the two bracketing keyframes
        kfs = self._keyframes
        prev_kf: Optional[Keyframe] = None
        next_kf: Optional[Keyframe] = None

        for kf in kfs:
            if kf.t <= now:
                prev_kf = kf
            else:
                next_kf = kf
                break

        if prev_kf is not None and next_kf is not None:
            d = _interp_keyframes(prev_kf, next_kf, now)
            params.viseme_index  = d["viseme_index"]
            params.viseme_weight = d["viseme_weight"]
            params.emotion_a     = d["emotion_a"]
            params.emotion_b     = d["emotion_b"]
            params.emotion_blend = d["emotion_blend"]
            params.head_yaw      = d["head_yaw"]
            params.head_pitch    = d["head_pitch"]
            params.head_roll     = d["head_roll"]
        elif prev_kf is not None:
            # Past the last keyframe — hold final pose, fade viseme to rest
            fade = max(0.0, 1.0 - (now - prev_kf.t) / 0.12)
            params.viseme_index  = prev_kf.viseme_index
            params.viseme_weight = prev_kf.viseme_weight * fade
            params.emotion_a     = prev_kf.emotion_a
            params.emotion_b     = prev_kf.emotion_b
            params.emotion_blend = prev_kf.emotion_blend
            params.head_yaw      = prev_kf.head_yaw
            params.head_pitch    = prev_kf.head_pitch
            params.head_roll     = prev_kf.head_roll

        # Prune stale keyframes (keep last 2 for reference)
        if len(kfs) > 4:
            past_indices = [i for i, kf in enumerate(kfs) if kf.t < now - 2.0]
            if len(past_indices) > 1:
                self._keyframes = kfs[past_indices[-1]:]

        # Apply idle motion
        iy, ip, ir = self._idle.get(now)
        params.idle_yaw   = iy
        params.idle_pitch = ip
        params.idle_roll  = ir

        # Apply blink
        params.blink = self._blinker.get_weight(now)

        return params
