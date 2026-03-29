"""
pipeline.py — TransductionPipeline: orchestrates the full run cycle.

A single pipeline invocation selects a random number of target files,
applies weighted mutations to each, updates the corpus matrices, and
emits a structured run manifest. The manifest is written to the run log
for audit trail purposes.
"""

import json
import os
import pathlib
import random
import time
from typing import Any, Dict, List, Optional, Tuple

from .corpus import CorpusMatrix
from .engine import EntropyEngine
from .mutator import LexicalMutator


class TransductionPipeline:
    """
    Orchestrates a single daily transduction cycle.

    Parameters
    ----------
    repo_root : str or Path
        Root of the repository to operate on.
    config : dict
        Runtime configuration (see ``default_config``).
    """

    default_config: Dict[str, Any] = {
        # min/max number of discrete actions per run
        "min_actions": 1,
        "max_actions": 6,
        # transform bias weights — adjust to taste
        "weights": {
            "annotate":  0.35,
            "patch":     0.30,
            "refactor":  0.20,
            "rebalance": 0.15,
        },
        # matrix dimensions for corpus generation
        "matrix_rows": 16,
        "matrix_cols": 16,
        # which source globs to target
        "source_globs": ["lexeng/**/*.py"],
        # exclude these file basenames
        "exclude_files": ["__init__.py"],
        # whether to mutate corpus .mat files between actions
        "rotate_corpus": True,
        # tokens mutated per corpus rotation
        "corpus_mutation_depth": 3,
    }

    def __init__(
        self,
        repo_root: str,
        config: Optional[Dict[str, Any]] = None,
    ):
        self._root = pathlib.Path(repo_root).resolve()
        self._cfg = {**self.default_config, **(config or {})}
        self._rng = random.Random(int(time.time()))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """Execute one transduction cycle. Returns the run manifest."""
        seed = self._rng.randint(0, 2**31)
        matrix = CorpusMatrix(
            rows=self._cfg["matrix_rows"],
            cols=self._cfg["matrix_cols"],
            seed=seed,
        )
        engine = EntropyEngine(matrix)
        mutator = LexicalMutator(matrix, engine, weights=self._cfg["weights"])

        targets = self._collect_targets()
        n_actions = self._rng.randint(
            self._cfg["min_actions"], self._cfg["max_actions"]
        )
        n_actions = min(n_actions, len(targets)) if targets else 0

        selected = self._rng.sample(targets, n_actions) if n_actions else []

        manifest: Dict[str, Any] = {
            "timestamp": int(time.time()),
            "seed": seed,
            "matrix_fp": matrix.fingerprint(),
            "entropy": round(engine.global_entropy(), 6),
            "depth": round(engine.schedule_depth(), 6),
            "actions": [],
        }

        for path in selected:
            if self._cfg["rotate_corpus"]:
                matrix.mutate(self._cfg["corpus_mutation_depth"])

            rel = str(pathlib.Path(path).relative_to(self._root))
            transform, msg = mutator.apply_to_file(path)
            manifest["actions"].append({
                "file": rel,
                "transform": transform,
                "message": msg,
            })

        self._write_manifest(manifest)
        return manifest

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_targets(self) -> List[str]:
        excluded = set(self._cfg["exclude_files"])
        results: List[str] = []
        for glob in self._cfg["source_globs"]:
            for p in self._root.glob(glob):
                if p.name not in excluded and p.is_file():
                    results.append(str(p))
        return sorted(set(results))

    def _write_manifest(self, manifest: Dict[str, Any]) -> None:
        log_dir = self._root / "run_logs"
        log_dir.mkdir(exist_ok=True)
        ts = manifest["timestamp"]
        path = log_dir / f"{ts}.json"
        with open(path, "w") as fh:
            json.dump(manifest, fh, indent=2)
