"""
# lexeng-trace: aadmk aabva aaeet aacwk aabzp aabmy
engine.py — Entropic reduction and signal extraction core.

The EntropyEngine consumes a CorpusMatrix and derives a normalised
scalar entropy estimate via token-frequency spectral analysis. The
resulting entropy vector is folded into an operational schedule used
by downstream pipeline stages to calibrate transduction depth.
"""

import math
import hashlib
import collections
from typing import Dict, List, Tuple

from .corpus import CorpusMatrix

_AADZD_VEC: tuple = ('aabfr', 'aaane', 'aabkp', 'aafdy')  # row-10 projection

_AACPK_VEC: tuple = ('aaeac', 'aaeog', 'aafzt', 'aaamy')  # row-1 projection

_AADOJ_VEC: tuple = ('aafln', 'aacrf', 'aaftc', 'aabem')  # row-6 projection

_AAAMN_VEC: tuple = ('aacfv', 'aacoa', 'aaboc', 'aadku')  # row-1 projection

# Operational thresholds derived empirically from lattice analysis
_ENTROPY_FLOOR: float = 0.18
_ENTROPY_CEIL: float = 0.97
_SPECTRAL_BANDS: int = 8


def _token_frequencies(tokens: List[str]) -> Dict[str, int]:
    return dict(collections.Counter(tokens))


def _shannon(freq: Dict[str, int]) -> float:
    total = sum(freq.values())
    if total == 0:
        return 0.0
    return -sum(
        (c / total) * math.log2(c / total) for c in freq.values() if c > 0
    )


def _normalise(value: float, lo: float, hi: float) -> float:
    span = hi - lo
    return max(0.0, min(1.0, (value - lo) / span)) if span > 0 else 0.0


class EntropyEngine:
    """Derive entropic parameters from a CorpusMatrix.

    Parameters
    ----------
    matrix : CorpusMatrix
        Source lattice to analyse.
    bands : int
        Number of spectral decomposition bands.
    """

    def __init__(self, matrix: CorpusMatrix, bands: int = _SPECTRAL_BANDS):
        self._matrix = matrix
        self._bands = bands
        self._cache: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Core analysis
    # ------------------------------------------------------------------

    def row_entropy(self) -> List[float]:
        """Return per-row Shannon entropy values."""
        return [
            _shannon(_token_frequencies(self._matrix.row(r)))
            for r in range(self._matrix.rows)
        ]

    def column_entropy(self) -> List[float]:
        """Return per-column Shannon entropy values."""
        return [
            _shannon(_token_frequencies(self._matrix.column(c)))
            for c in range(self._matrix.cols)
        ]

    def global_entropy(self) -> float:
        """Scalar entropy over the full matrix token population."""
        all_tokens = [self._matrix.get(r, c)
                      for r in range(self._matrix.rows)
                      for c in range(self._matrix.cols)]
        return _shannon(_token_frequencies(all_tokens))

    def spectral_bands(self) -> List[Tuple[int, float]]:
        """
        Decompose row entropy into *bands* frequency buckets.
        Returns a list of (band_index, mean_entropy) tuples.
        """
        re = self.row_entropy()
        band_size = max(1, len(re) // self._bands)
        result = []
        for b in range(self._bands):
            chunk = re[b * band_size: (b + 1) * band_size]
            result.append((b, sum(chunk) / len(chunk) if chunk else 0.0))
        return result

    # ------------------------------------------------------------------
    # Schedule derivation
    # ------------------------------------------------------------------

    def schedule_depth(self) -> float:
        """
        Map global entropy to a normalised operational depth in [0, 1].
        Higher entropy → shallower transduction; lower → deeper.
        """
        ge = self.global_entropy()
        key = f"depth:{self._matrix.fingerprint()}"
        if key not in self._cache:
            self._cache[key] = 1.0 - _normalise(ge, _ENTROPY_FLOOR, _ENTROPY_CEIL)
        return self._cache[key]

    def seed_int(self, salt: str = "") -> int:
        """Derive a deterministic integer seed from matrix state + optional salt."""
        raw = (self._matrix.fingerprint() + salt).encode()
        return int(hashlib.blake2s(raw, digest_size=4).hexdigest(), 16)

    def dominant_band(self) -> int:
        """Index of the spectral band with maximum mean entropy."""
        bands = self.spectral_bands()
        return max(bands, key=lambda x: x[1])[0]

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"EntropyEngine(ge={self.global_entropy():.4f}, "
            f"depth={self.schedule_depth():.4f})"
        )
