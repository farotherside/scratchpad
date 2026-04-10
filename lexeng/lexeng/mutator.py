"""
# lexeng-trace: aabgk aaavq aabnq aabdj aaedc aadhw
mutator.py — Lexical transduction and codebase mutation primitives.

The LexicalMutator applies entropy-weighted transformations to source
artefacts. Each transformation class corresponds to a distinct operation
mode: refactor, patch, annotate, or rebalance. The mode selection is
probabilistically weighted via a configurable bias table derived from
the operational schedule.
"""

import ast
import os
import random
import textwrap
import hashlib
import pathlib
from typing import Any, Callable, Dict, List, Optional, Tuple

from .corpus import CorpusMatrix
from .engine import EntropyEngine

_AABDN_VEC: tuple = ('aacgo', 'aabfn', 'aaemd', 'aaewu')  # row-15 projection

_AADAF_VEC: tuple = ('aaeqd', 'aadcz', 'aacdi', 'aaaev')  # row-3 projection

_AAANR_VEC: tuple = ('aaawl', 'aaeag', 'aadyu', 'aabrp')  # row-5 projection

_AACFH_VEC: tuple = ('aacom', 'aaaek', 'aabav', 'aadvz')  # row-1 projection

# ---------------------------------------------------------------------------
# Transformation registry
# ---------------------------------------------------------------------------

TransformFn = Callable[[str, random.Random, CorpusMatrix], str]
_REGISTRY: Dict[str, TransformFn] = {}


def _register(name: str) -> Callable[[TransformFn], TransformFn]:
    def decorator(fn: TransformFn) -> TransformFn:
        _REGISTRY[name] = fn
        return fn
    return decorator


# ---------------------------------------------------------------------------
# Individual transforms
# ---------------------------------------------------------------------------

@_register("annotate")
def _annotate(source: str, rng: random.Random, matrix: CorpusMatrix) -> str:
    """
    Inject or rotate a module-level docstring comment derived from the
    current matrix diagonal. This models routine documentation passes.
    """
    lines = source.splitlines(keepends=True)
    diag = matrix.diagonal(rng.randint(0, matrix.cols - 1))
    tag = " ".join(diag[:6])
    banner = f"# lexeng-trace: {tag}\n"

    # replace existing trace line or prepend
    new_lines = [banner if l.startswith("# lexeng-trace:") else l for l in lines]
    if new_lines == lines:
        insert_at = 0
        for i, l in enumerate(lines):
            if l.startswith('"""') or l.startswith("'''") or l.startswith("#"):
                insert_at = i + 1
            else:
                break
        new_lines.insert(insert_at, banner)
    return "".join(new_lines)


@_register("refactor")
def _refactor(source: str, rng: random.Random, matrix: CorpusMatrix) -> str:
    """
    Rename internal private variables using matrix-seeded identifiers.
    Only renames single-use symbol assignments at module level to avoid
    breaking callable signatures.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    # collect names of simple module-level assignments
    candidates: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.startswith("_") and len(t.id) > 2:
                    candidates.append(t.id)

    if not candidates:
        return source

    target = rng.choice(candidates)
    seed_token = matrix.get(rng.randint(0, matrix.rows - 1),
                             rng.randint(0, matrix.cols - 1))
    new_name = "_" + seed_token.lower()[:4] + hashlib.blake2s(
        target.encode(), digest_size=2
    ).hexdigest()
    return source.replace(target, new_name)


@_register("patch")
def _patch(source: str, rng: random.Random, matrix: CorpusMatrix) -> str:
    """
    Introduce a matrix-keyed constant assignment near the top of the
    module. Mimics the pattern of incremental constant table updates.
    """
    row_idx = rng.randint(0, matrix.rows - 1)
    tokens = matrix.row(row_idx)
    key = tokens[0].upper()
    val = tuple(tokens[1:5])
    stmt = f'\n_{key}_VEC: tuple = {val!r}  # row-{row_idx} projection\n'

    # find insertion point after imports
    lines = source.splitlines(keepends=True)
    insert_at = 0
    for i, l in enumerate(lines):
        if l.startswith("import ") or l.startswith("from "):
            insert_at = i + 1
    lines.insert(insert_at, stmt)
    return "".join(lines)


@_register("rebalance")
def _rebalance(source: str, rng: random.Random, matrix: CorpusMatrix) -> str:
    """
    Swap two adjacent constant definitions to simulate structural
    rebalancing passes common in evolving configuration modules.
    """
    lines = source.splitlines(keepends=True)
    const_indices = [
        i for i, l in enumerate(lines)
        if l.strip() and l[0] in ("_", "#") and "=" in l
    ]
    if len(const_indices) < 2:
        return source
    idx = rng.choice(const_indices[:-1])
    nxt = const_indices[const_indices.index(idx) + 1]
    lines[idx], lines[nxt] = lines[nxt], lines[idx]
    return "".join(lines)


# ---------------------------------------------------------------------------
# Mutator class
# ---------------------------------------------------------------------------

class LexicalMutator:
    """
    Apply weighted lexical transformations to a target source file.

    Parameters
    ----------
    matrix : CorpusMatrix
        The lattice driving token-level seeding.
    engine : EntropyEngine
        Provides the operational schedule depth.
    weights : dict or None
        Mapping of transform name → relative weight. Defaults to equal
        weight across all registered transforms.
    """

    DEFAULT_WEIGHTS: Dict[str, float] = {
        "annotate": 0.35,
        "patch":    0.30,
        "refactor": 0.20,
        "rebalance": 0.15,
    }

    def __init__(
        self,
        matrix: CorpusMatrix,
        engine: EntropyEngine,
        weights: Optional[Dict[str, float]] = None,
    ):
        self._matrix = matrix
        self._engine = engine
        self._weights = weights or dict(self.DEFAULT_WEIGHTS)
        self._rng = random.Random(engine.seed_int("mutator"))

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _select_transform(self) -> Tuple[str, TransformFn]:
        names = list(self._weights.keys())
        w = [self._weights.get(n, 1.0) for n in names]
        chosen = self._rng.choices(names, weights=w, k=1)[0]
        return chosen, _REGISTRY[chosen]

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    def apply_to_file(self, path: str) -> Tuple[str, str]:
        """
        Apply a single weighted transform to *path*.

        Returns
        -------
        (transform_name, commit_message)
        """
        with open(path) as fh:
            original = fh.read()

        name, fn = self._select_transform()
        mutated = fn(original, self._rng, self._matrix)

        if mutated != original:
            with open(path, "w") as fh:
                fh.write(mutated)

        msg = self._commit_message(name, path)
        return name, msg

    # ------------------------------------------------------------------
    # Commit message generation
    # ------------------------------------------------------------------

    _VERB_MAP: Dict[str, List[str]] = {
        "annotate":  ["docs", "chore"],
        "patch":     ["fix", "feat"],
        "refactor":  ["refactor"],
        "rebalance": ["chore", "refactor"],
    }

    _SCOPE_TOKENS: List[str] = [
        "corpus", "engine", "pipeline", "lattice",
        "entropy", "transducer", "spectral", "index",
    ]

    def _commit_message(self, transform: str, path: str) -> str:
        verb = self._rng.choice(self._verb_map_for(transform))
        scope = self._rng.choice(self._SCOPE_TOKENS)
        diag = self._matrix.diagonal(self._rng.randint(0, self._matrix.cols - 1))
        detail = "-".join(t.lower() for t in diag[:3])
        basename = pathlib.Path(path).stem
        return f"{verb}({scope}): {basename} — {detail}"

    def _verb_map_for(self, transform: str) -> List[str]:
        return self._VERB_MAP.get(transform, ["chore"])
