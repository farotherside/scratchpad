"""
mesh.py — OBJ loader and blend-shape vertex deformer for the teen_head model.

Coordinate system (post-normalisation):
  +X  right,  +Y  up,  +Z  toward camera
  Head spans roughly  X ∈ [-0.71, 0.71]
                      Y ∈ [-0.92, 1.00]
                      Z ∈ [-0.77, 0.71]
  Nose tip:  Y ≈ -0.14,  Z ≈  0.71
  Mouth:     Y ≈ -0.45 … -0.05,  Z > 0.51 (centre-line)
  Brows:     Y ≈  0.21 …  0.62,  Z ≈  0.53
  Jaw:       Y ≈ -0.74 … -0.35,  Z  0.20 … 0.58
"""

from __future__ import annotations
import numpy as np
from pathlib import Path

ASSETS_DIR = Path(__file__).parent.parent / "assets"


# ---------------------------------------------------------------------------
# OBJ loader
# ---------------------------------------------------------------------------

def _load_obj(path: str):
    """
    Parse an OBJ file with v / vn / f records.
    Returns:
      verts        (V, 3)  float32  — vertex positions (original units)
      tri_v        (F, 3)  int32   — triangulated face vertex indices (0-based)
      vert_normals (V, 3)  float32 — smooth per-vertex normals
    """
    raw_verts: list   = []
    raw_normals: list = []
    face_v: list      = []   # (F, 3) vertex index triples
    face_n: list      = []   # (F, 3) normal index triples

    with open(path) as fh:
        for line in fh:
            if line.startswith("v "):
                raw_verts.append(list(map(float, line.split()[1:4])))
            elif line.startswith("vn "):
                raw_normals.append(list(map(float, line.split()[1:4])))
            elif line.startswith("f "):
                parts = line.split()[1:]
                vids, nids = [], []
                for p in parts:
                    tok = p.split("/")
                    vids.append(int(tok[0]) - 1)
                    nids.append(int(tok[2]) - 1 if len(tok) > 2 and tok[2] else 0)
                # Fan triangulation (handles quads and n-gons)
                for i in range(1, len(vids) - 1):
                    face_v.append([vids[0], vids[i], vids[i + 1]])
                    face_n.append([nids[0], nids[i], nids[i + 1]])

    verts      = np.array(raw_verts,   dtype=np.float32)
    obj_normals = np.array(raw_normals, dtype=np.float32)
    tri_v      = np.array(face_v,      dtype=np.int32)
    tri_n      = np.array(face_n,      dtype=np.int32)

    # Build smooth per-vertex normals by averaging OBJ normals at each vertex
    vert_normals = np.zeros_like(verts)
    np.add.at(vert_normals, tri_v[:, 0], obj_normals[tri_n[:, 0]])
    np.add.at(vert_normals, tri_v[:, 1], obj_normals[tri_n[:, 1]])
    np.add.at(vert_normals, tri_v[:, 2], obj_normals[tri_n[:, 2]])
    mag = np.linalg.norm(vert_normals, axis=1, keepdims=True)
    vert_normals /= np.where(mag > 0, mag, 1.0)

    return verts, tri_v, vert_normals


# ---------------------------------------------------------------------------
# Gaussian influence field
# ---------------------------------------------------------------------------

def _gauss(verts: np.ndarray, center, radius: float) -> np.ndarray:
    """Soft Gaussian weight around a 3-D point. Returns (V,) in [0, 1]."""
    d2 = ((verts - np.array(center, np.float32)) ** 2).sum(axis=1)
    return np.exp(-d2 / (radius * radius))


# ---------------------------------------------------------------------------
# Grid-based mesh decimation (run once at load time)
# ---------------------------------------------------------------------------

def _decimate(
    verts: np.ndarray,
    faces: np.ndarray,
    normals: np.ndarray,
    grid: int = 10,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Cluster vertices into a uniform grid, merge each cluster to its centroid,
    and remove degenerate triangles.  Reduces 16 K triangles → ~1 K.
    """
    v_min = verts.min(axis=0)
    v_max = verts.max(axis=0)
    step  = (v_max - v_min) / grid
    step  = np.where(step > 0, step, 1.0)   # avoid div-by-zero on flat dims

    gi = np.clip(((verts - v_min) / step).astype(np.int32), 0, grid - 1)
    cluster_id = gi[:, 0] * (grid * grid) + gi[:, 1] * grid + gi[:, 2]

    unique_ids, inv = np.unique(cluster_id, return_inverse=True)
    nv = len(unique_ids)

    # Centroid and averaged normal per cluster
    new_v = np.zeros((nv, 3), np.float32)
    new_n = np.zeros((nv, 3), np.float32)
    counts = np.zeros(nv, np.float32)
    np.add.at(new_v, inv, verts)
    np.add.at(new_n, inv, normals)
    np.add.at(counts, inv, 1.0)
    new_v /= counts[:, None]
    mag = np.linalg.norm(new_n, axis=1, keepdims=True)
    new_n /= np.where(mag > 0, mag, 1.0)

    # Remap face indices and drop degenerates
    new_f = inv[faces]
    v0, v1, v2 = new_f[:, 0], new_f[:, 1], new_f[:, 2]
    valid = (v0 != v1) & (v1 != v2) & (v0 != v2)
    return new_v, new_f[valid].astype(np.int32), new_n


# ---------------------------------------------------------------------------
# MeshFace
# ---------------------------------------------------------------------------

class MeshFace:
    """
    Loaded mesh with blend-shape support.

    Call get_deformed(params) each frame to obtain vertex positions and normals
    with blend shapes applied.  The base mesh and influence weights are
    precomputed at construction time.
    """

    # Grid resolution for load-time decimation.
    # grid=10 → ~1 000 triangles (vs 16 K raw) — fast enough for 15 FPS.
    DECIMATE_GRID = 10

    def __init__(self, obj_path: str | None = None):
        path = obj_path or str(ASSETS_DIR / "teen_head.obj")
        verts, faces, vert_normals = _load_obj(path)

        # Centre on vertex centroid, scale so the largest half-extent = 1
        self._centroid = verts.mean(axis=0)
        vc = verts - self._centroid
        self._scale = float(np.abs(vc).max())
        vn = (vc / self._scale).astype(np.float32)

        # Decimate to ~1 K triangles so the rasteriser loop is fast
        vn, faces, vert_normals = _decimate(vn, faces, vert_normals,
                                            self.DECIMATE_GRID)

        self._base_verts   = vn              # (V, 3)
        self._base_normals = vert_normals    # (V, 3)
        self.faces         = faces           # (F, 3) int32

        self._build_weights(vn)
        self._build_material_ids(vn)

    # ------------------------------------------------------------------
    # Material IDs
    # 0 = skin (default)
    # 1 = eye socket / iris
    # 2 = eyebrow
    # 3 = lips
    # 4 = inner mouth / teeth
    # ------------------------------------------------------------------
    MAT_SKIN  = 0
    MAT_EYE   = 1
    MAT_BROW  = 2
    MAT_LIP   = 3
    MAT_MOUTH = 4

    # Base luminance multiplier per material (applied on top of Phong)
    MAT_LUM = {
        0: 1.00,   # skin — full shading
        1: 0.30,   # eye socket — dark
        2: 0.45,   # eyebrow — dark-ish
        3: 0.75,   # lips — slightly muted
        4: 0.90,   # inner mouth — near-white when visible
    }

    def _build_material_ids(self, v: np.ndarray):
        """Assign a material ID to each vertex by dominant Gaussian region."""
        n = len(v)
        mat = np.zeros(n, dtype=np.int32)   # default: skin

        # Narrower Gaussians for material classification (tighter than morphs)
        w_eye   = (_gauss(v, [ 0.23,  0.22, 0.63], 0.09) +
                   _gauss(v, [-0.23,  0.22, 0.63], 0.09))
        w_brow  = (_gauss(v, [ 0.25,  0.40, 0.55], 0.10) +
                   _gauss(v, [-0.25,  0.40, 0.55], 0.10))
        w_lip   = (_gauss(v, [ 0.00, -0.08, 0.70], 0.10) +
                   _gauss(v, [ 0.00, -0.22, 0.68], 0.10))
        w_mouth = _gauss(v,  [ 0.00, -0.15, 0.64], 0.07)

        # Assign by highest weight, in priority order
        # (mouth interior is inside lip region so check first)
        mat[w_mouth > 0.4]                        = self.MAT_MOUTH
        mat[(w_lip   > 0.4) & (mat == 0)]         = self.MAT_LIP
        mat[(w_eye   > 0.35) & (mat == 0)]        = self.MAT_EYE
        mat[(w_brow  > 0.35) & (mat == 0)]        = self.MAT_BROW

        self._vert_mat = mat   # (V,) int32

    # ------------------------------------------------------------------
    def _build_weights(self, v: np.ndarray):
        """
        Precompute per-vertex Gaussian influence weights for each morph.
        All anchors are in normalised coordinate space.
        """
        # --- Mouth / jaw -----------------------------------------------
        # Upper lip moves up on mouth_open
        self._w_upper_lip   = _gauss(v, [ 0.00, -0.08, 0.68], 0.12)
        # Lower lip moves down on mouth_open
        self._w_lower_lip   = _gauss(v, [ 0.00, -0.22, 0.66], 0.13)
        # Jaw/chin bulk drops on jaw_drop and mouth_open
        self._w_jaw         = _gauss(v, [ 0.00, -0.55, 0.32], 0.34)
        # Lip corners for smile / wide / frown
        self._w_corn_r      = _gauss(v, [ 0.13, -0.175, 0.63], 0.11)
        self._w_corn_l      = _gauss(v, [-0.13, -0.175, 0.63], 0.11)

        # --- Eyes / brows ----------------------------------------------
        self._w_brow_r      = _gauss(v, [ 0.25,  0.40,  0.53], 0.14)
        self._w_brow_l      = _gauss(v, [-0.25,  0.40,  0.53], 0.14)
        self._w_eye_r       = _gauss(v, [ 0.23,  0.22,  0.61], 0.11)
        self._w_eye_l       = _gauss(v, [-0.23,  0.22,  0.61], 0.11)

        # --- Cheeks ----------------------------------------------------
        self._w_cheek_r     = _gauss(v, [ 0.38, -0.04,  0.53], 0.19)
        self._w_cheek_l     = _gauss(v, [-0.38, -0.04,  0.53], 0.19)

    # ------------------------------------------------------------------
    def get_deformed(self, params) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Apply blend shapes from FaceParams.
        Returns (verts, faces, normals) — verts/normals are fresh copies.
        """
        v = self._base_verts.copy()

        vm         = params.get_viseme_shape()
        mouth_open = float(vm[0])
        mouth_wide = float(vm[1])

        smile  = float(params.get_morph("mouth_smile"))
        frown  = float(params.get_morph("mouth_frown"))
        brow_r = float(params.get_morph("brow_raise"))
        brow_f = float(params.get_morph("brow_furrow"))
        cheek  = float(params.get_morph("cheek_raise"))
        eye_w  = float(params.get_morph("eye_wide"))
        eye_sq = float(params.get_morph("eye_squint"))
        jaw_d  = float(params.get_morph("jaw_drop"))

        # --- Mouth open / jaw drop -------------------------------------
        drop = jaw_d + mouth_open * 0.5
        v[:, 1] -= self._w_jaw       * drop       * 0.14
        v[:, 1] -= self._w_lower_lip * mouth_open * 0.07
        v[:, 1] += self._w_upper_lip * mouth_open * 0.03

        # --- Mouth wide ------------------------------------------------
        v[:, 0] += self._w_corn_r * mouth_wide * 0.06
        v[:, 0] -= self._w_corn_l * mouth_wide * 0.06

        # --- Smile (corners up + out) ----------------------------------
        v[:, 1] += self._w_corn_r * smile * 0.05
        v[:, 1] += self._w_corn_l * smile * 0.05
        v[:, 0] += self._w_corn_r * smile * 0.04
        v[:, 0] -= self._w_corn_l * smile * 0.04

        # --- Frown (corners down) --------------------------------------
        v[:, 1] -= self._w_corn_r * frown * 0.05
        v[:, 1] -= self._w_corn_l * frown * 0.05

        # --- Brow raise ------------------------------------------------
        brows = self._w_brow_r + self._w_brow_l
        v[:, 1] += brows * brow_r * 0.08

        # --- Brow furrow (in + down) -----------------------------------
        v[:, 1] -= brows          * brow_f * 0.04
        v[:, 0] -= self._w_brow_r * brow_f * 0.04   # right brow pulls left
        v[:, 0] += self._w_brow_l * brow_f * 0.04   # left  brow pulls right

        # --- Cheek raise -----------------------------------------------
        ck = max(cheek, smile * 0.25)
        cheeks = self._w_cheek_r + self._w_cheek_l
        v[:, 1] += cheeks * ck * 0.05
        v[:, 2] += cheeks * ck * 0.03

        # --- Eye wide / squint -----------------------------------------
        eyes = self._w_eye_r + self._w_eye_l
        v[:, 1] += eyes * eye_w  * 0.04
        v[:, 1] -= eyes * eye_sq * 0.03

        # Base normals are close enough for the small deformations used
        return v, self.faces, self._base_normals, self._vert_mat
