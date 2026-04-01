"""
renderer.py — Vectorised SDF ray marcher producing a float32 luminance framebuffer.

All geometry is defined as Signed Distance Functions (SDF).  The ray marcher
steps along each pixel's ray until it hits a surface (dist < EPSILON) or
exhausts MAX_STEPS, then applies Phong shading to compute luminance [0, 1].

No external dependencies beyond numpy.
"""

import math
import numpy as np
from core.face_model import FaceParams

# ---------------------------------------------------------------------------
# Ray marching constants
# ---------------------------------------------------------------------------
MAX_STEPS = 48
MAX_DIST = 6.0
EPSILON = 0.005

# ---------------------------------------------------------------------------
# vec3 helpers (operate on (..., 3) arrays)
# ---------------------------------------------------------------------------

def _length(v):
    return np.sqrt((v * v).sum(axis=-1, keepdims=True))

def _normalize(v):
    return v / (np.sqrt((v * v).sum(axis=-1, keepdims=True)) + 1e-12)

def _dot(a, b):
    return (a * b).sum(axis=-1, keepdims=True)


# ---------------------------------------------------------------------------
# SDF primitives (all operate on (..., 3) position arrays, return (..., 1))
# ---------------------------------------------------------------------------

def sdf_sphere(p, centre, radius):
    return _length(p - centre) - radius

def sdf_ellipsoid(p, centre, radii):
    q = (p - centre) / radii
    return (_length(q) - 1.0) * np.min(radii)

def sdf_box(p, centre, half_extents):
    q = np.abs(p - centre) - half_extents
    return (
        _length(np.maximum(q, 0.0)) +
        np.minimum(np.max(q, axis=-1, keepdims=True), 0.0)
    )

def sdf_capsule(p, a, b, radius):
    """Capsule between points a and b with given radius."""
    ab = b - a
    ap = p - a
    t = np.clip((_dot(ap, ab)) / (_dot(ab, ab) + 1e-12), 0.0, 1.0)
    return _length(ap - t * ab) - radius

def sdf_torus(p, centre, big_r, small_r):
    q = p - centre
    xz = q[..., [0, 2]]
    y  = q[..., [1]]
    ring = np.sqrt((xz * xz).sum(axis=-1, keepdims=True)) - big_r
    return np.sqrt(ring * ring + y * y) - small_r


# ---------------------------------------------------------------------------
# Boolean ops
# ---------------------------------------------------------------------------
def sdf_union(a, b):        return np.minimum(a, b)
def sdf_subtract(a, b):     return np.maximum(a, -b)
def sdf_intersect(a, b):    return np.maximum(a, b)

def sdf_smooth_union(a, b, k=0.1):
    # Inigo Quilez smooth minimum: mix(b, a, h) - k*h*(1-h)
    # h→1 when close to a, h→0 when close to b
    h = np.clip(0.5 + 0.5 * (b - a) / k, 0.0, 1.0)
    return b * (1 - h) + a * h - k * h * (1 - h)


# ---------------------------------------------------------------------------
# Rotation helpers
# ---------------------------------------------------------------------------

def _rot_x(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float32)

def _rot_y(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float32)

def _rot_z(angle):
    c, s = math.cos(angle), math.sin(angle)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float32)


def _apply_rotation(p, yaw, pitch, roll):
    """Rotate point cloud p by yaw/pitch/roll (head pose)."""
    R = _rot_y(yaw) @ _rot_x(pitch) @ _rot_z(roll)
    return p @ R.T


# ---------------------------------------------------------------------------
# Face SDF — assembled from primitives + FaceParams blend shapes
# ---------------------------------------------------------------------------

def face_sdf(p: np.ndarray, params: FaceParams) -> np.ndarray:
    """
    Evaluate the complete face SDF at every point in p.
    p has shape (..., 3).  Returns (..., 1) distance values.
    """
    # Rotate world-space points into head-local space (inverse head pose)
    p_local = _apply_rotation(p, -params.total_yaw, -params.total_pitch, -params.total_roll)

    # Pull viseme blend values
    vm = params.get_viseme_shape()
    mouth_open   = vm[0]
    mouth_wide   = vm[1]
    lip_round    = vm[2]

    # Pull emotion blend values
    smile  = params.get_morph("mouth_smile")
    frown  = params.get_morph("mouth_frown")
    brow_r = params.get_morph("brow_raise")
    brow_f = params.get_morph("brow_furrow")
    cheek  = params.get_morph("cheek_raise")
    eye_w  = params.get_morph("eye_wide")
    eye_sq = params.get_morph("eye_squint")
    jaw_d  = params.get_morph("jaw_drop")
    blink  = params.blink

    # --- HEAD (slightly squashed sphere → ellipsoid) ---
    head = sdf_ellipsoid(p_local, np.array([0, 0, 0], dtype=np.float32),
                         np.array([0.75, 0.88, 0.78], dtype=np.float32))

    # --- JAW --- lower half of head drops slightly when mouth opens
    jaw_offset = jaw_d * 0.06 + mouth_open * 0.08
    jaw = sdf_ellipsoid(p_local, np.array([0, -0.35 - jaw_offset, 0.05], dtype=np.float32),
                        np.array([0.62, 0.42, 0.60], dtype=np.float32))
    head = sdf_smooth_union(head, jaw, 0.08)

    # --- NOSE ---
    nose_tip = sdf_sphere(p_local, np.array([0, -0.08, 0.72], dtype=np.float32), 0.11)
    nose_bridge = sdf_capsule(p_local,
                              np.array([0,  0.18, 0.65], dtype=np.float32),
                              np.array([0, -0.02, 0.73], dtype=np.float32), 0.06)
    nose = sdf_smooth_union(nose_tip, nose_bridge, 0.05)
    head = sdf_smooth_union(head, nose, 0.04)

    # --- EYES ---
    eye_base_y = 0.15 + brow_r * 0.02 - eye_sq * 0.02
    eye_r_sz   = np.array([0.14, 0.09 + eye_w * 0.03 - eye_sq * 0.04, 0.10], dtype=np.float32)
    eye_l_sz   = eye_r_sz.copy()

    eye_r = sdf_ellipsoid(p_local, np.array([ 0.28, eye_base_y, 0.62], dtype=np.float32), eye_r_sz)
    eye_l = sdf_ellipsoid(p_local, np.array([-0.28, eye_base_y, 0.62], dtype=np.float32), eye_l_sz)
    eyes  = sdf_union(eye_r, eye_l)

    # --- EYELIDS (blink — thin box sweeping over eye) ---
    if blink > 0.02:
        lid_h = blink * 0.13
        lid_r = sdf_box(p_local,
                        np.array([ 0.28, eye_base_y + 0.06 - lid_h * 0.5, 0.63], dtype=np.float32),
                        np.array([0.16, lid_h, 0.05], dtype=np.float32))
        lid_l = sdf_box(p_local,
                        np.array([-0.28, eye_base_y + 0.06 - lid_h * 0.5, 0.63], dtype=np.float32),
                        np.array([0.16, lid_h, 0.05], dtype=np.float32))
        lids  = sdf_union(lid_r, lid_l)
        head  = sdf_smooth_union(head, lids, 0.03)

    # Carve eyes out of head
    head = sdf_subtract(head, eyes)

    # --- EYEBROWS ---
    brow_y = 0.33 + brow_r * 0.06 - brow_f * 0.04
    brow_inner_tilt = brow_f * 0.04 - brow_r * 0.01

    brow_r_center = np.array([ 0.27, brow_y + brow_inner_tilt, 0.67], dtype=np.float32)
    brow_l_center = np.array([-0.27, brow_y + brow_inner_tilt, 0.67], dtype=np.float32)
    brow_r_sdf = sdf_ellipsoid(p_local, brow_r_center, np.array([0.17, 0.035, 0.05], dtype=np.float32))
    brow_l_sdf = sdf_ellipsoid(p_local, brow_l_center, np.array([0.17, 0.035, 0.05], dtype=np.float32))
    brows = sdf_union(brow_r_sdf, brow_l_sdf)
    head  = sdf_smooth_union(head, brows, 0.03)

    # --- MOUTH ---
    # Lip centre descends with jaw_drop; widens with mouth_wide + smile
    lip_y     = -0.38 - jaw_d * 0.05
    lip_w_x   = 0.22 + mouth_wide * 0.12 + smile * 0.10
    smile_y   = smile * 0.04 - frown * 0.04

    # Upper lip
    up_lip = sdf_ellipsoid(p_local,
                           np.array([0, lip_y + 0.055 + mouth_open * 0.04 + smile_y, 0.67], dtype=np.float32),
                           np.array([lip_w_x, 0.04 + lip_round * 0.02, 0.06], dtype=np.float32))
    # Lower lip
    lo_lip = sdf_ellipsoid(p_local,
                           np.array([0, lip_y - 0.055 - mouth_open * 0.06 + smile_y, 0.67], dtype=np.float32),
                           np.array([lip_w_x * 0.95, 0.05 + lip_round * 0.02, 0.06], dtype=np.float32))
    # Mouth cavity (subtract from head when open)
    if mouth_open > 0.05:
        cavity = sdf_ellipsoid(p_local,
                               np.array([0, lip_y + smile_y, 0.66], dtype=np.float32),
                               np.array([lip_w_x * 0.85, mouth_open * 0.12, 0.08], dtype=np.float32))
        head = sdf_subtract(head, cavity)

    lips = sdf_union(up_lip, lo_lip)
    head = sdf_smooth_union(head, lips, 0.03)

    # --- CHEEKS --- (puff slightly when smiling)
    if cheek > 0.01 or smile > 0.1:
        ck = max(cheek, smile * 0.3)
        ck_r = sdf_ellipsoid(p_local,
                             np.array([ 0.55, -0.10, 0.58], dtype=np.float32),
                             np.array([0.18 + ck * 0.08, 0.16, 0.14], dtype=np.float32))
        ck_l = sdf_ellipsoid(p_local,
                             np.array([-0.55, -0.10, 0.58], dtype=np.float32),
                             np.array([0.18 + ck * 0.08, 0.16, 0.14], dtype=np.float32))
        head = sdf_smooth_union(head, sdf_union(ck_r, ck_l), 0.06)

    # --- EARS ---
    ear_r = sdf_ellipsoid(p_local, np.array([ 0.78, 0.02, -0.05], dtype=np.float32),
                          np.array([0.07, 0.18, 0.10], dtype=np.float32))
    ear_l = sdf_ellipsoid(p_local, np.array([-0.78, 0.02, -0.05], dtype=np.float32),
                          np.array([0.07, 0.18, 0.10], dtype=np.float32))
    head = sdf_smooth_union(head, sdf_union(ear_r, ear_l), 0.04)

    return head


# ---------------------------------------------------------------------------
# Normal estimation via central differences
# ---------------------------------------------------------------------------
_NORM_EPS = 0.004
_NORM_OFFS = np.array([
    [ _NORM_EPS,  0,  0],
    [-_NORM_EPS,  0,  0],
    [0,  _NORM_EPS,  0],
    [0, -_NORM_EPS,  0],
    [0,  0,  _NORM_EPS],
    [0,  0, -_NORM_EPS],
], dtype=np.float32)  # (6, 3)


def estimate_normal(hit_pts: np.ndarray, params: FaceParams) -> np.ndarray:
    """hit_pts: (N, 3)  →  returns (N, 3) unit normals."""
    N = hit_pts.shape[0]
    # Broadcast: (N, 6, 3)
    p6 = hit_pts[:, None, :] + _NORM_OFFS[None, :, :]   # (N, 6, 3)
    p6_flat = p6.reshape(-1, 3)
    d = face_sdf(p6_flat, params).reshape(N, 6)
    nx = d[:, 0] - d[:, 1]
    ny = d[:, 2] - d[:, 3]
    nz = d[:, 4] - d[:, 5]
    n = np.stack([nx, ny, nz], axis=-1)
    # per-row normalisation
    return n / (np.sqrt((n * n).sum(axis=-1, keepdims=True)) + 1e-12)


# ---------------------------------------------------------------------------
# Ray marcher
# ---------------------------------------------------------------------------

def render(width: int, height: int, params: FaceParams) -> np.ndarray:
    """
    Render the face into a float32 luminance framebuffer of shape (height, width).
    Values are in [0, 1].
    """
    # Pixel grid in NDC space [-1, 1] x [-1, 1], corrected for aspect ratio
    aspect = width / height
    xs = np.linspace(-aspect, aspect, width, dtype=np.float32)
    ys = np.linspace( 1.0,   -1.0,   height, dtype=np.float32)
    xv, yv = np.meshgrid(xs, ys)               # (H, W)

    # Camera: sits on +Z axis looking toward origin
    cam_pos = np.array([0.0, 0.0, 2.8], dtype=np.float32)
    focal_len = 1.8

    # Ray directions (perspective) — normalise per-pixel (keepdims on last axis)
    rd = np.stack([xv, yv, np.full_like(xv, -focal_len)], axis=-1)   # (H, W, 3)
    rd_len = np.sqrt((rd * rd).sum(axis=-1, keepdims=True)) + 1e-12
    rd = rd / rd_len

    # Flatten to (N, 3)
    N = height * width
    ray_o = np.broadcast_to(cam_pos, (N, 3)).copy()
    ray_d = rd.reshape(N, 3)

    # Ray march — operate on ALL rays each step using full-array masking
    t      = np.zeros(N, dtype=np.float32)
    hit    = np.zeros(N, dtype=bool)
    active = np.ones(N,  dtype=bool)

    for _ in range(MAX_STEPS):
        if not active.any():
            break
        # Evaluate SDF at all active ray positions
        active_idx = np.where(active)[0]
        pts = ray_o[active_idx] + ray_d[active_idx] * t[active_idx, None]
        d = face_sdf(pts, params).reshape(-1)
        # Advance active rays
        t[active_idx] += d
        # Mark hits
        newly_hit = active_idx[d < EPSILON]
        hit[newly_hit] = True
        active[newly_hit] = False
        # Deactivate rays that escaped
        escaped = active_idx[t[active_idx] > MAX_DIST]
        active[escaped] = False

    # Shade hit points
    luminance = np.zeros(N, dtype=np.float32)

    if hit.any():
        hit_pts = ray_o[hit] + ray_d[hit] * t[hit, None]
        normals = estimate_normal(hit_pts, params)

        # Phong lighting
        light1 = _normalize_vec(np.array([ 1.5,  2.0,  3.0], dtype=np.float32))
        light2 = _normalize_vec(np.array([-1.0,  0.5,  2.0], dtype=np.float32))
        # Normalise per-row (each ray direction is a separate vector)
        vd = -ray_d[hit]
        view_dir = vd / (np.linalg.norm(vd, axis=-1, keepdims=True) + 1e-12)

        # Diffuse
        diff1 = np.clip((normals * light1).sum(axis=-1), 0, 1) * 0.7
        diff2 = np.clip((normals * light2).sum(axis=-1), 0, 1) * 0.2
        # Ambient
        amb = 0.12
        # Specular
        refl1 = normals * 2.0 * np.clip((normals * light1).sum(axis=-1, keepdims=True), 0, 1) - light1
        spec1 = np.clip((refl1 * view_dir).sum(axis=-1), 0, 1) ** 16 * 0.25

        luminance[hit] = np.clip(amb + diff1 + diff2 + spec1, 0.0, 1.0)

    return luminance.reshape(height, width)


def _normalize_vec(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-12)
