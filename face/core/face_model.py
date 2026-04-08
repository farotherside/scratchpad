"""
face_model.py — Blend-shape parameter definitions for the 3D face.

All weights are floats in [0.0, 1.0] unless noted.
The renderer samples these at frame-time to position / scale SDF primitives.
"""

from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Viseme groups
# Loosely based on Preston Blair phoneme groups, reduced for terminal resolution
# ---------------------------------------------------------------------------
#
#  Group 0 (rest / neutral)   : silence, consonants with closed mouth
#  Group 1 (wide open)        : AH, AA, AW  "father", "lot", "law"
#  Group 2 (mid open)         : AE, EH      "cat", "bed"
#  Group 3 (ee smile)         : IY, IH, EY  "feet", "bit", "bait"
#  Group 4 (oh round)         : OW, UW, OY  "go", "boot", "boy"
#  Group 5 (lips together)    : M, B, P
#  Group 6 (teeth visible)    : F, V
#  Group 7 (th)               : TH, DH
#  Group 8 (tongue tip)       : T, D, N, L  (slight open, tip visible)

CHAR_TO_VISEME: dict[str, int] = {
    # silence / default
    " ": 0, ",": 0, ".": 0, "!": 0, "?": 0, ";": 0, ":": 0,

    # group 1 — wide open
    "a": 1,

    # group 2 — mid open
    "e": 2,

    # group 3 — ee smile
    "i": 3, "y": 3,

    # group 4 — oh round
    "o": 4, "u": 4, "w": 4,

    # group 5 — lips together
    "m": 5, "b": 5, "p": 5,

    # group 6 — teeth visible
    "f": 6, "v": 6,

    # group 7 — th
    # (no single-char shorthand; mapped by the lipsync layer)

    # group 8 — tongue tip
    "t": 8, "d": 8, "n": 8, "l": 8,
}

# Blend weights per viseme group
# Each tuple: (mouth_open, mouth_wide, lip_round, upper_teeth, lower_teeth, tongue_out)
VISEME_SHAPES: list[tuple[float, float, float, float, float, float]] = [
    (0.00, 0.00, 0.00, 0.00, 0.00, 0.00),  # 0 rest
    (0.90, 0.20, 0.00, 0.10, 0.10, 0.00),  # 1 AH
    (0.50, 0.30, 0.00, 0.10, 0.05, 0.00),  # 2 EH
    (0.20, 0.80, 0.00, 0.10, 0.00, 0.00),  # 3 EE
    (0.50, 0.00, 0.80, 0.00, 0.10, 0.00),  # 4 OH
    (0.00, 0.00, 0.00, 0.00, 0.00, 0.00),  # 5 MB  (closed but jaw tensed — same as rest for now)
    (0.10, 0.10, 0.00, 0.90, 0.20, 0.00),  # 6 FV
    (0.15, 0.10, 0.00, 0.30, 0.30, 0.90),  # 7 TH
    (0.20, 0.20, 0.00, 0.50, 0.10, 0.50),  # 8 TDN
]


# ---------------------------------------------------------------------------
# Emotion blend shapes
# Each dict maps a named morph target to a weight.
# The renderer applies a weighted blend at eval time.
# ---------------------------------------------------------------------------
EMOTION_SHAPES: dict[str, dict[str, float]] = {
    "neutral": {
        "brow_raise": 0.0,
        "brow_furrow": 0.0,
        "cheek_raise": 0.0,
        "mouth_smile": 0.0,
        "mouth_frown": 0.0,
        "eye_wide": 0.0,
        "eye_squint": 0.0,
        "jaw_drop": 0.0,
    },
    "happy": {
        "brow_raise": 0.2,
        "brow_furrow": 0.0,
        "cheek_raise": 0.8,
        "mouth_smile": 0.9,
        "mouth_frown": 0.0,
        "eye_wide": 0.0,
        "eye_squint": 0.6,
        "jaw_drop": 0.1,
    },
    "sad": {
        "brow_raise": 0.0,
        "brow_furrow": 0.6,
        "cheek_raise": 0.0,
        "mouth_smile": 0.0,
        "mouth_frown": 0.8,
        "eye_wide": 0.0,
        "eye_squint": 0.2,
        "jaw_drop": 0.0,
    },
    "surprised": {
        "brow_raise": 1.0,
        "brow_furrow": 0.0,
        "cheek_raise": 0.0,
        "mouth_smile": 0.0,
        "mouth_frown": 0.0,
        "eye_wide": 1.0,
        "eye_squint": 0.0,
        "jaw_drop": 0.8,
    },
    "angry": {
        "brow_raise": 0.0,
        "brow_furrow": 1.0,
        "cheek_raise": 0.0,
        "mouth_smile": 0.0,
        "mouth_frown": 0.5,
        "eye_wide": 0.2,
        "eye_squint": 0.5,
        "jaw_drop": 0.1,
    },
}

EMOTIONS = list(EMOTION_SHAPES.keys())


# ---------------------------------------------------------------------------
# Runtime parameter block passed to renderer each frame
# ---------------------------------------------------------------------------
@dataclass
class FaceParams:
    # --- Emotion (blended between two states) ---
    emotion_a: str = "neutral"
    emotion_b: str = "neutral"
    emotion_blend: float = 0.0     # 0 = 100% emotion_a, 1 = 100% emotion_b

    # --- Viseme ---
    viseme_index: int = 0
    viseme_weight: float = 0.0     # 0 = rest, 1 = full viseme shape

    # --- Head pose (radians) ---
    head_yaw: float = 0.0          # left/right
    head_pitch: float = 0.0        # up/down
    head_roll: float = 0.0         # tilt

    # --- Blink ---
    blink: float = 0.0             # 0 = open, 1 = fully closed

    # --- Idle micro-motion (filled by animator) ---
    idle_yaw: float = 0.0
    idle_pitch: float = 0.0
    idle_roll: float = 0.0

    # --- Idle eye/mouth oscillation (filled by animator in idle mode) ---
    _idle_eye_close:  float = 0.0
    _idle_mouth_open: float = 0.0

    def get_morph(self, key: str) -> float:
        """Blend emotion morph targets + idle oscillation weights."""
        a = EMOTION_SHAPES[self.emotion_a].get(key, 0.0)
        b = EMOTION_SHAPES[self.emotion_b].get(key, 0.0)
        base = a + (b - a) * self.emotion_blend
        # Layer idle oscillations on top
        if key == "eye_squint":
            base = min(1.0, base + self._idle_eye_close)
        elif key == "jaw_drop":
            base = min(1.0, base + self._idle_mouth_open)
        return base

    def get_viseme_shape(self) -> tuple[float, float, float, float, float, float]:
        """Interpolate between rest (0) and target viseme shape."""
        rest = VISEME_SHAPES[0]
        target = VISEME_SHAPES[self.viseme_index]
        w = self.viseme_weight
        return tuple(r + (t - r) * w for r, t in zip(rest, target))  # type: ignore

    @property
    def total_yaw(self) -> float:
        return self.head_yaw + self.idle_yaw

    @property
    def total_pitch(self) -> float:
        return self.head_pitch + self.idle_pitch

    @property
    def total_roll(self) -> float:
        return self.head_roll + self.idle_roll
