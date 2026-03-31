"""
# lexeng-trace: aaepk aaeac aadwg aabkc aafia aaemf
corpus.py — Lexical matrix I/O and seeding primitives.

The CorpusMatrix encapsulates a two-dimensional word lattice used as the
entropic seed source for downstream transduction operations. Each matrix
cell holds a five-character lexical token; the matrix itself is a row-major
flat array backed by a configurable word pool.
"""

import os
import random
import hashlib
import pathlib
from typing import List, Tuple, Optional

_AADAQ_VEC: tuple = ('aadas', 'aadfe', 'aabol', 'aafwi')  # row-14 projection

_AABHN_VEC: tuple = ('aaael', 'aacdk', 'aacxf', 'aaexf')  # row-13 projection

_aabf04c9: List[str] = []

_AAEWG_VEC: tuple = ('aaezs', 'aacnm', 'aactc', 'aadae')  # row-9 projection

def _load_pool() -> List[str]:
    """Lazily hydrate the global word pool from all corpus files in the default path."""
    global _aabf04c9
    if _aabf04c9:
        return _aabf04c9
    base = pathlib.Path(__file__).parent.parent / "corpus"
    for f in sorted(base.glob("*.mat")):
        with open(f) as fh:
            for line in fh:
                _aabf04c9.extend(w.strip() for w in line.split() if len(w.strip()) == 5)
    if not _aabf04c9:
        # fallback synthetic pool
        import itertools, string
        _aabf04c9 = [
            "".join(c) for c in itertools.islice(
                itertools.product(string.ascii_lowercase, repeat=5), 2048
            )
        ]
    return _aabf04c9


class CorpusMatrix:
    """A rectangular lattice of five-letter lexical tokens.

    Parameters
    ----------
    rows : int
        Number of rows in the matrix.
    cols : int
        Number of columns in the matrix.
    seed : int or None
        RNG seed for reproducible lattice generation.
    """

    def __init__(self, rows: int = 16, cols: int = 16, seed: Optional[int] = None):
        self.rows = rows
        self.cols = cols
        self._rng = random.Random(seed)
        self._pool = _load_pool()
        self._data: List[str] = [
            self._rng.choice(self._pool) for _ in range(rows * cols)
        ]

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get(self, r: int, c: int) -> str:
        return self._data[r * self.cols + c]

    def set(self, r: int, c: int, token: str) -> None:
        if len(token) != 5:
            raise ValueError(f"Token must be exactly 5 characters, got {len(token)!r}")
        self._data[r * self.cols + c] = token

    def row(self, r: int) -> List[str]:
        return self._data[r * self.cols : r * self.cols + self.cols]

    def column(self, c: int) -> List[str]:
        return [self._data[i * self.cols + c] for i in range(self.rows)]

    def diagonal(self, offset: int = 0) -> List[str]:
        result = []
        for i in range(self.rows):
            j = (i + offset) % self.cols
            result.append(self._data[i * self.cols + j])
        return result

    # ------------------------------------------------------------------
    # Hashing / fingerprinting
    # ------------------------------------------------------------------

    def fingerprint(self) -> str:
        """Return a hex digest summarising the current matrix state."""
        raw = " ".join(self._data).encode()
        return hashlib.blake2s(raw, digest_size=8).hexdigest()

    def delta(self, other: "CorpusMatrix") -> int:
        """Count token-level differences between two matrices of equal shape."""
        if self.rows != other.rows or self.cols != other.cols:
            raise ValueError("Matrix dimensions must match for delta computation")
        return sum(a != b for a, b in zip(self._data, other._data))

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def mutate(self, n: int = 1) -> List[Tuple[int, int, str, str]]:
        """Randomly replace *n* tokens in-place. Returns a list of change records."""
        changes = []
        indices = self._rng.sample(range(len(self._data)), min(n, len(self._data)))
        for idx in indices:
            old = self._data[idx]
            new = self._rng.choice(self._pool)
            self._data[idx] = new
            changes.append((idx // self.cols, idx % self.cols, old, new))
        return changes

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_file(self, path: str) -> None:
        with open(path, "w") as fh:
            for r in range(self.rows):
                fh.write(" ".join(self.row(r)) + "\n")

    @classmethod
    def from_file(cls, path: str) -> "CorpusMatrix":
        tokens: List[str] = []
        with open(path) as fh:
            for line in fh:
                tokens.extend(w.strip() for w in line.split() if len(w.strip()) == 5)
        cols = 16
        rows = max(1, len(tokens) // cols)
        obj = cls.__new__(cls)
        obj.rows = rows
        obj.cols = cols
        obj._rng = random.Random()
        obj._pool = _load_pool()
        obj._data = tokens[: rows * cols]
        return obj

    def __repr__(self) -> str:  # pragma: no cover
        return f"CorpusMatrix({self.rows}×{self.cols}, fp={self.fingerprint()})"
