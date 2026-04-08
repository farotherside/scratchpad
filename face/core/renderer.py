"""
renderer.py — Mesh rasteriser for the teen_head OBJ model.

Replaces the SDF ray marcher.  Pipeline per frame:
  1. Apply blend-shape deformation  (numpy, vectorised)
  2. Apply head-pose rotation        (numpy, vectorised)
  3. Perspective-project vertices    (numpy, vectorised)
  4. Backface-cull + bbox filter     (numpy, vectorised)
  5. Per-triangle rasterise + z-buf  (Python loop over visible tris only)
  6. Phong shade hit pixels           (numpy, vectorised)

No external dependencies beyond numpy.
"""

from __future__ import annotations
import math
import numpy as np
from core.face_model import FaceParams
from core.mesh import MeshFace

# ---------------------------------------------------------------------------
# Lazy singleton mesh (loaded once on first render call)
# ---------------------------------------------------------------------------
_MESH: MeshFace | None = None
_MESH_MODEL: str | None = None


def _get_mesh(model: str | None = None) -> MeshFace:
    global _MESH, _MESH_MODEL
    if _MESH is None or model != _MESH_MODEL:
        _MESH = MeshFace(model=model)
        _MESH_MODEL = model
    return _MESH


# ---------------------------------------------------------------------------
# Camera / projection constants
# ---------------------------------------------------------------------------
CAM_Z = 2.0
FOCAL = 1.8

# Terminal characters are ~2× taller than wide in pixels.
# Compensate so the face fills the screen proportionally.
CHAR_ASPECT = 0.5    # char_pixel_width / char_pixel_height

# ---------------------------------------------------------------------------
# Rotation helpers (same convention as original renderer)
# ---------------------------------------------------------------------------

def _rot_y(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], np.float32)


def _rot_x(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], np.float32)


def _rot_z(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], np.float32)


# ---------------------------------------------------------------------------
# Lighting
# ---------------------------------------------------------------------------
def _unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-12)


_L1 = _unit(np.array([ 1.2,  1.8,  2.5], np.float32))   # key
_L2 = _unit(np.array([-0.8,  0.4,  1.5], np.float32))   # fill
_VIEW = _unit(np.array([0.0,  0.0,  1.0], np.float32))   # camera direction


def _shade(normals: np.ndarray) -> np.ndarray:
    """normals: (N, 3) unit → luminance (N,) in [0, 1]"""
    diff1 = np.clip(normals @ _L1, 0.0, 1.0) * 0.65
    diff2 = np.clip(normals @ _L2, 0.0, 1.0) * 0.20
    amb   = 0.15
    h1    = _unit(_L1 + _VIEW)
    spec  = np.clip(normals @ h1, 0.0, 1.0) ** 20 * 0.18
    return np.clip(amb + diff1 + diff2 + spec, 0.0, 1.0)


# ---------------------------------------------------------------------------
# Perspective projection:  world (V,3) → screen pixel coords (V,2) + depth (V,)
# ---------------------------------------------------------------------------

def _project(verts: np.ndarray, width: int, height: int):
    """
    Returns (px, py, pz) each shape (V,).
      px in [0, 1],  py in [0, 1],  pz = Z world (higher = closer to camera)
    Applies character-aspect correction so the face fills the terminal.
    """
    w  = np.clip(CAM_Z - verts[:, 2], 1e-3, None)
    # Effective aspect accounts for character pixel dimensions
    eff_aspect = (width * CHAR_ASPECT) / height
    sx = verts[:, 0] / w * FOCAL
    sy = verts[:, 1] / w * FOCAL
    px = (sx / eff_aspect + 1.0) * 0.5    # [0, 1]
    py = (1.0 - sy)               * 0.5   # [0, 1]  y flipped
    return px, py, verts[:, 2]


# ---------------------------------------------------------------------------
# Rasteriser
# ---------------------------------------------------------------------------

def _rasterise(
    px: np.ndarray, py: np.ndarray, pz: np.ndarray,
    vert_normals: np.ndarray,
    vert_mat: np.ndarray,
    faces: np.ndarray,
    width: int, height: int,
) -> np.ndarray:
    """
    Rasterise triangles into a (height, width) float32 luminance framebuffer.

    px, py    — vertex screen coords in [0, 1]
    pz        — vertex world Z  (higher = closer to camera)
    vert_mat  — per-vertex material ID (int32)
    """
    # Scale to pixel coords
    spx = px * width
    spy = py * height

    V0 = faces[:, 0]; V1 = faces[:, 1]; V2 = faces[:, 2]

    x0 = spx[V0]; y0 = spy[V0]; z0 = pz[V0]
    x1 = spx[V1]; y1 = spy[V1]; z1 = pz[V1]
    x2 = spx[V2]; y2 = spy[V2]; z2 = pz[V2]

    # -------------------------------------------------------------------
    # Vectorised pre-pass: backface cull + screen bbox
    # -------------------------------------------------------------------
    # Screen-space signed area (positive = CCW in screen = CW in world)
    area2 = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0)

    # Backface cull using world-space face normal dot with view direction.
    # Cross product of edges in world space, check Z component > 0 (faces +Z camera).
    n = vert_normals
    # Average normal of the three vertices as a proxy for face orientation
    nz_avg = (n[V0, 2] + n[V1, 2] + n[V2, 2]) / 3.0
    front_facing = nz_avg > -0.1   # generous threshold keeps edge-on faces

    # Screen bbox (integer, clamped)
    xmin_f = np.minimum(np.minimum(x0, x1), x2)
    xmax_f = np.maximum(np.maximum(x0, x1), x2)
    ymin_f = np.minimum(np.minimum(y0, y1), y2)
    ymax_f = np.maximum(np.maximum(y0, y1), y2)

    bx0 = np.clip(np.floor(xmin_f).astype(np.int32), 0, width  - 1)
    bx1 = np.clip(np.ceil( xmax_f).astype(np.int32), 0, width  - 1)
    by0 = np.clip(np.floor(ymin_f).astype(np.int32), 0, height - 1)
    by1 = np.clip(np.ceil( ymax_f).astype(np.int32), 0, height - 1)

    abs_area = np.abs(area2)
    non_empty = (bx0 <= bx1) & (by0 <= by1) & (abs_area > 1e-4)
    visible   = np.where(front_facing & non_empty)[0]

    # -------------------------------------------------------------------
    # Z-buffer + normal buffer + material buffer
    # Higher pz = closer to camera, so initialise to -inf
    # -------------------------------------------------------------------
    zbuf  = np.full((height, width), -np.inf, np.float32)
    nbuf  = np.zeros((height, width, 3), np.float32)
    mbuf  = np.zeros((height, width), np.int32)   # material IDs

    for fi in visible:
        i0, i1, i2 = int(V0[fi]), int(V1[fi]), int(V2[fi])

        ax, ay, az = float(x0[fi]), float(y0[fi]), float(z0[fi])
        bx, by_, bz = float(x1[fi]), float(y1[fi]), float(z1[fi])
        cx_, cy, cz = float(x2[fi]), float(y2[fi]), float(z2[fi])
        a2 = float(area2[fi])
        inv_a = 1.0 / a2   # signed inverse area

        xa, xb = int(bx0[fi]), int(bx1[fi])
        ya, yb = int(by0[fi]), int(by1[fi])

        bw = xb - xa + 1
        bh = yb - ya + 1

        # Pixel-centre coordinates for the bounding box
        gx = (np.arange(xa, xb + 1, dtype=np.float32) + 0.5)  # (bw,)
        gy = (np.arange(ya, yb + 1, dtype=np.float32) + 0.5)  # (bh,)
        gx2d, gy2d = np.meshgrid(gx, gy)   # (bh, bw)

        # Barycentric weights via sub-triangle signed areas.
        # w0 = area(v1,v2,p)/area2,  where area(a,b,p)=(b-a)×(p-a)
        # inv_a = 1/area2 (signed; same as sign/abs_area)
        w0 = ((cx_ - bx) * (gy2d - by_) - (cy - by_) * (gx2d - bx))  * inv_a
        w1 = ((ax - cx_) * (gy2d - cy)  - (ay - cy)  * (gx2d - cx_)) * inv_a
        w2 = 1.0 - w0 - w1

        inside = (w0 >= 0.0) & (w1 >= 0.0) & (w2 >= 0.0)
        if not inside.any():
            continue

        iz = w0 * az + w1 * bz + w2 * cz

        # Z-test: only update if this triangle is closer (higher z)
        cur_z  = zbuf[ya:yb + 1, xa:xb + 1]
        update = inside & (iz > cur_z)
        if not update.any():
            continue

        zbuf[ya:yb + 1, xa:xb + 1][update] = iz[update]

        # Interpolate normals
        n0 = n[i0]; n1 = n[i1]; n2 = n[i2]
        ni = w0[..., None] * n0 + w1[..., None] * n1 + w2[..., None] * n2
        mag = np.sqrt((ni * ni).sum(axis=-1, keepdims=True))
        ni /= np.where(mag > 0, mag, 1.0)
        nbuf[ya:yb + 1, xa:xb + 1][update] = ni[update]

        # Material: pick dominant vertex (highest barycentric weight)
        m0, m1, m2 = int(vert_mat[i0]), int(vert_mat[i1]), int(vert_mat[i2])
        # Use the material of the vertex with the highest avg bary weight
        tri_mat = m0 if m0 == m1 or m0 == m2 else (m1 if m1 == m2 else m0)
        mbuf[ya:yb + 1, xa:xb + 1][update] = tri_mat

    # -------------------------------------------------------------------
    # Shade hit pixels — Phong shading modulated by per-material luminance
    # -------------------------------------------------------------------
    from core.mesh import MeshFace
    mat_lum = MeshFace.MAT_LUM

    luminance = np.zeros((height, width), np.float32)
    hit = np.isfinite(zbuf)
    if hit.any():
        phong = _shade(nbuf[hit])                          # [0, 1]
        mids  = mbuf[hit]                                  # material IDs
        scale = np.array([mat_lum[m] for m in mids], dtype=np.float32)
        luminance[hit] = np.clip(phong * scale, 0.0, 1.0)
    return luminance, zbuf


# ---------------------------------------------------------------------------
# Public entry point — same signature as the old SDF renderer
# ---------------------------------------------------------------------------

def render(width: int, height: int, params: FaceParams,
           return_depth: bool = False, model: str | None = None):
    """
    Returns float32 luminance framebuffer (height, width) in [0, 1].
    If *return_depth* is True, returns (luminance, zbuf) where zbuf holds
    per-pixel world-Z values (-inf for background, higher = closer to camera).
    *model* selects the OBJ mesh (default "generic").
    """
    mesh = _get_mesh(model)
    verts, faces, vert_normals, vert_mat = mesh.get_deformed(params)

    # Head-pose rotation (same convention as old renderer)
    R = _rot_y(params.total_yaw) @ _rot_x(params.total_pitch) @ _rot_z(params.total_roll)
    verts_r   = verts        @ R.T
    normals_r = vert_normals @ R.T

    px, py, pz = _project(verts_r, width, height)

    luminance, zbuf = _rasterise(px, py, pz, normals_r, vert_mat, faces, width, height)
    if return_depth:
        return luminance, zbuf
    return luminance
