"""
renderer.py — Vectorised SDF ray marcher with per-material shading.

Each face primitive carries a material ID so features are distinguishable
at terminal resolution via luminance contrast, not geometric detail alone.

Materials:
  0 = background (black)
  1 = skin
  2 = eye (dark brown / near black)
  3 = eyebrow (dark)
  4 = lip (slightly darker / pinker than skin)
  5 = teeth/inner mouth (near white when open, else dark)
  6 = ear

No external dependencies beyond numpy.
"""

import math
import numpy as np
from core.face_model import FaceParams

# ---------------------------------------------------------------------------
# Ray marching constants
# ---------------------------------------------------------------------------
MAX_STEPS = 64
MAX_DIST  = 6.0
EPSILON   = 0.003

# Material base luminances (before lighting)
MAT_BASE = {
    0: 0.00,   # background
    1: 0.82,   # skin
    2: 0.08,   # eye (iris/pupil — very dark)
    3: 0.18,   # eyebrow
    4: 0.62,   # lips (noticeably darker than skin)
    5: 0.90,   # inner mouth / teeth
    6: 0.78,   # ear (slightly darker than face skin)
}

# How much lighting contributes per material (skin = full, eye = minimal)
MAT_LIGHT = {
    0: 0.0,
    1: 1.0,
    2: 0.15,
    3: 0.5,
    4: 0.7,
    5: 0.4,
    6: 0.9,
}

# ---------------------------------------------------------------------------
# vec3 helpers
# ---------------------------------------------------------------------------

def _len2(v):
    """Per-row length, shape (...,) from (..., 3)."""
    return np.sqrt((v * v).sum(axis=-1))

def _len2k(v):
    """Per-row length keepdims, shape (...,1) from (...,3)."""
    return np.sqrt((v * v).sum(axis=-1, keepdims=True))

def _norm(v):
    return v / (_len2k(v) + 1e-12)

def _dot(a, b):
    return (a * b).sum(axis=-1)


# ---------------------------------------------------------------------------
# SDF primitives  — return scalar distance arrays shape (N,)
# ---------------------------------------------------------------------------

def _sphere(p, c, r):
    return _len2(p - c) - r

def _ellipsoid(p, c, radii):
    q = (p - c) / radii
    return (_len2(q) - 1.0) * float(np.min(radii))

def _capsule(p, a, b, r):
    ab = b - a
    ap = p - a
    t  = np.clip((ap * ab).sum(axis=-1) / ((ab * ab).sum() + 1e-12), 0.0, 1.0)
    return _len2(ap - t[:, None] * ab) - r

def _box(p, c, he):
    q = np.abs(p - c) - he
    return _len2(np.maximum(q, 0.0)) + np.minimum(q.max(axis=-1), 0.0)

def _smin(a, b, k=0.08):
    """Smooth minimum (IQ), returns blended distance."""
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1 - h)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _rot_y(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32)

def _rot_x(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)

def _rot_z(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)

def _rotate(p, yaw, pitch, roll):
    R = _rot_y(yaw) @ _rot_x(pitch) @ _rot_z(roll)
    return p @ R.T


# ---------------------------------------------------------------------------
# Multi-material scene evaluation
# Returns (dist, mat_id) both shape (N,)
# ---------------------------------------------------------------------------

def _scene(p: np.ndarray, params: FaceParams):
    """Evaluate all primitives, return closest distance + material ID per point."""
    N = p.shape[0]

    # Apply inverse head pose to get head-local coordinates
    pl = _rotate(p, -params.total_yaw, -params.total_pitch, -params.total_roll)

    # Blend shape values
    vm = params.get_viseme_shape()
    mouth_open = vm[0]
    mouth_wide = vm[1]
    lip_round  = vm[2]

    smile  = params.get_morph("mouth_smile")
    frown  = params.get_morph("mouth_frown")
    brow_r = params.get_morph("brow_raise")
    brow_f = params.get_morph("brow_furrow")
    cheek  = params.get_morph("cheek_raise")
    eye_w  = params.get_morph("eye_wide")
    eye_sq = params.get_morph("eye_squint")
    jaw_d  = params.get_morph("jaw_drop")
    blink  = params.blink

    # Accumulate (dist, mat) — start with background
    best_d   = np.full(N, MAX_DIST, dtype=np.float32)
    best_mat = np.zeros(N, dtype=np.int32)

    def _update(d, mat_id):
        mask = d < best_d
        best_d[mask]   = d[mask]
        best_mat[mask] = mat_id

    # ── HEAD ────────────────────────────────────────────────────────────────
    head = _ellipsoid(pl, [0, 0, 0], np.array([0.72, 0.88, 0.76], np.float32))

    # Jaw drop
    jaw_cy = -0.35 - jaw_d * 0.05 - mouth_open * 0.06
    jaw    = _ellipsoid(pl, np.array([0, jaw_cy, 0.05], np.float32),
                        np.array([0.60, 0.40, 0.58], np.float32))
    head = _smin(head, jaw, 0.10)

    # Cheeks
    ck = max(float(cheek), float(smile) * 0.25)
    if ck > 0.01:
        ck_r = _ellipsoid(pl, np.array([ 0.52, -0.12, 0.56], np.float32),
                          np.array([0.20 + ck * 0.06, 0.17, 0.14], np.float32))
        ck_l = _ellipsoid(pl, np.array([-0.52, -0.12, 0.56], np.float32),
                          np.array([0.20 + ck * 0.06, 0.17, 0.14], np.float32))
        head = _smin(head, np.minimum(ck_r, ck_l), 0.07)

    # Nose tip
    nose_tip = _sphere(pl, np.array([0, -0.06, 0.74], np.float32), 0.10)
    nose_br  = _capsule(pl,
                        np.array([0,  0.20, 0.66], np.float32),
                        np.array([0, -0.02, 0.74], np.float32), 0.055)
    nose = np.minimum(nose_tip, nose_br)
    head = _smin(head, nose, 0.04)

    # Ears
    ear_r = _ellipsoid(pl, np.array([ 0.76, 0.02, -0.05], np.float32),
                       np.array([0.07, 0.17, 0.09], np.float32))
    ear_l = _ellipsoid(pl, np.array([-0.76, 0.02, -0.05], np.float32),
                       np.array([0.07, 0.17, 0.09], np.float32))
    ears = np.minimum(ear_r, ear_l)

    # Skin = head + ears
    skin_d = np.minimum(head, ears)
    _update(skin_d, 1)

    # ── EYES ────────────────────────────────────────────────────────────────
    eye_y  = 0.15 + brow_r * 0.02 - eye_sq * 0.02
    eye_rx = np.array([0.14, 0.10 + eye_w * 0.03 - eye_sq * 0.04, 0.09], np.float32)
    eye_r  = _ellipsoid(pl, np.array([ 0.26, eye_y, 0.66], np.float32), eye_rx)
    eye_l  = _ellipsoid(pl, np.array([-0.26, eye_y, 0.66], np.float32), eye_rx)
    eyes_d = np.minimum(eye_r, eye_l)
    _update(eyes_d, 2)

    # Eyelids (blink) — skin-coloured box sweeping down over eye
    if blink > 0.02:
        lid_h = blink * 0.14
        lid_r = _box(pl, np.array([ 0.26, eye_y + 0.07 - lid_h * 0.5, 0.67], np.float32),
                     np.array([0.17, lid_h, 0.05], np.float32))
        lid_l = _box(pl, np.array([-0.26, eye_y + 0.07 - lid_h * 0.5, 0.67], np.float32),
                     np.array([0.17, lid_h, 0.05], np.float32))
        lids_d = np.minimum(lid_r, lid_l)
        _update(lids_d, 1)   # skin material

    # ── EYEBROWS ────────────────────────────────────────────────────────────
    brow_y = 0.34 + brow_r * 0.07 - brow_f * 0.04
    brow_tilt = brow_f * 0.05
    br_r = _ellipsoid(pl, np.array([ 0.26, brow_y + brow_tilt, 0.68], np.float32),
                      np.array([0.16, 0.038, 0.045], np.float32))
    br_l = _ellipsoid(pl, np.array([-0.26, brow_y + brow_tilt, 0.68], np.float32),
                      np.array([0.16, 0.038, 0.045], np.float32))
    brows_d = np.minimum(br_r, br_l)
    _update(brows_d, 3)

    # ── LIPS ────────────────────────────────────────────────────────────────
    lip_y   = -0.37 - jaw_d * 0.04
    lip_w   = 0.20 + mouth_wide * 0.10 + smile * 0.09
    smile_y =  smile * 0.04 - frown * 0.04

    up_lip = _ellipsoid(pl,
                        np.array([0, lip_y + 0.062 + mouth_open * 0.03 + smile_y, 0.69], np.float32),
                        np.array([lip_w, 0.042 + lip_round * 0.01, 0.055], np.float32))
    lo_lip = _ellipsoid(pl,
                        np.array([0, lip_y - 0.058 - mouth_open * 0.05 + smile_y, 0.69], np.float32),
                        np.array([lip_w * 0.92, 0.050 + lip_round * 0.01, 0.055], np.float32))
    lips_d = np.minimum(up_lip, lo_lip)
    _update(lips_d, 4)

    # Inner mouth / teeth (only when open enough to matter)
    if mouth_open > 0.08:
        inner = _ellipsoid(pl,
                           np.array([0, lip_y + smile_y, 0.66], np.float32),
                           np.array([lip_w * 0.80, mouth_open * 0.11, 0.07], np.float32))
        _update(inner, 5)

    return best_d, best_mat


# ---------------------------------------------------------------------------
# Normal estimation (central differences, per-material-agnostic distance)
# ---------------------------------------------------------------------------
_NEPS = 0.003
_NOFF = np.array([
    [ _NEPS, 0, 0], [-_NEPS, 0, 0],
    [0,  _NEPS, 0], [0, -_NEPS, 0],
    [0, 0,  _NEPS], [0, 0, -_NEPS],
], dtype=np.float32)


def _normals(pts: np.ndarray, params: FaceParams) -> np.ndarray:
    """pts: (N,3) → normals: (N,3)"""
    N = pts.shape[0]
    p6 = pts[:, None, :] + _NOFF[None, :, :]      # (N, 6, 3)
    d6, _ = _scene(p6.reshape(-1, 3), params)
    d6 = d6.reshape(N, 6)
    n = np.stack([d6[:, 0] - d6[:, 1],
                  d6[:, 2] - d6[:, 3],
                  d6[:, 4] - d6[:, 5]], axis=-1)
    return n / (np.sqrt((n * n).sum(axis=-1, keepdims=True)) + 1e-12)


# ---------------------------------------------------------------------------
# Main render function
# ---------------------------------------------------------------------------

def render(width: int, height: int, params: FaceParams) -> np.ndarray:
    """
    Returns float32 luminance framebuffer (height, width) in [0, 1].
    """
    aspect = width / height
    xs = np.linspace(-aspect, aspect, width,  dtype=np.float32)
    ys = np.linspace( 1.0,   -1.0,   height, dtype=np.float32)
    xv, yv = np.meshgrid(xs, ys)

    cam = np.array([0.0, 0.0, 2.8], dtype=np.float32)
    focal = 1.8

    # Ray directions — normalised per pixel
    rd = np.stack([xv, yv, np.full_like(xv, -focal)], axis=-1)  # (H, W, 3)
    rd = rd / (_len2k(rd) + 1e-12)

    N = height * width
    ray_o = np.broadcast_to(cam, (N, 3)).copy()
    ray_d = rd.reshape(N, 3)

    t        = np.zeros(N,  dtype=np.float32)
    hit      = np.zeros(N,  dtype=bool)
    hit_mat  = np.zeros(N,  dtype=np.int32)
    active   = np.ones(N,   dtype=bool)

    for _ in range(MAX_STEPS):
        if not active.any():
            break
        idx = np.where(active)[0]
        pts = ray_o[idx] + ray_d[idx] * t[idx, None]
        d, mat = _scene(pts, params)
        t[idx] += d
        newly_hit = idx[d < EPSILON]
        hit[newly_hit]     = True
        hit_mat[newly_hit] = mat[d < EPSILON]
        active[newly_hit]  = False
        active[idx[t[idx] > MAX_DIST]] = False

    # Shade
    luminance = np.zeros(N, dtype=np.float32)

    if hit.any():
        pts_hit = ray_o[hit] + ray_d[hit] * t[hit, None]
        nrm     = _normals(pts_hit, params)
        mat_ids = hit_mat[hit]

        L1  = _norm(np.array([ 1.2,  1.8,  2.5], np.float32)[None])
        L2  = _norm(np.array([-0.8,  0.4,  1.5], np.float32)[None])
        vd  = _norm(-ray_d[hit])

        diff1 = np.clip(_dot(nrm, L1), 0, 1) * 0.65
        diff2 = np.clip(_dot(nrm, L2), 0, 1) * 0.20
        amb   = 0.15

        refl  = nrm * 2.0 * np.clip(_dot(nrm, L1), 0, 1)[:, None] - L1
        spec  = np.clip(_dot(refl, vd), 0, 1) ** 20 * 0.18

        light = np.clip(amb + diff1 + diff2 + spec, 0.0, 1.0)

        # Per-material luminance
        base  = np.array([MAT_BASE[m] for m in mat_ids], dtype=np.float32)
        lscale = np.array([MAT_LIGHT[m] for m in mat_ids], dtype=np.float32)

        luminance[hit] = np.clip(base + (light - 0.5) * lscale * 0.6, 0.0, 1.0)

    return luminance.reshape(height, width)
