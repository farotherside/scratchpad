#!/usr/bin/env python3
"""
run.py — Lexical Entropy Engine entry point.

Executes one transduction cycle, then commits all mutations to git.
Designed for cron invocation; all output is written to run_logs/.

Usage
-----
    python run.py [--config config.json]

The script will:
  1. Run the TransductionPipeline
  2. Stage all modified files
  3. Emit one commit per action (using the generated commit message)
  4. Push to remote

Configuration
-------------
Pass --config pointing at a JSON file to override any key in
TransductionPipeline.default_config. Example:

    {
        "min_actions": 2,
        "max_actions": 8,
        "weights": {
            "annotate":  0.20,
            "patch":     0.40,
            "refactor":  0.30,
            "rebalance": 0.10
        }
    }
"""

import argparse
import json
import os
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).parent.resolve()

sys.path.insert(0, str(ROOT))
from lexeng.pipeline import TransductionPipeline


def _git(*args: str, cwd: str = str(ROOT)) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)!r} failed:\n{result.stderr.strip()}")
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Lexical Entropy Engine runner")
    parser.add_argument("--config", metavar="FILE", help="JSON config override")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run pipeline but skip git operations")
    args = parser.parse_args()

    config: dict = {}
    if args.config:
        with open(args.config) as fh:
            config = json.load(fh)

    pipeline = TransductionPipeline(repo_root=str(ROOT), config=config)
    manifest = pipeline.run()

    actions = manifest.get("actions", [])
    if not actions:
        print("[lexeng] No actions produced this cycle.")
        return 0

    print(f"[lexeng] {len(actions)} action(s) | entropy={manifest['entropy']} "
          f"| depth={manifest['depth']}")

    if args.dry_run:
        for a in actions:
            print(f"  [dry] {a['transform']:10s} {a['file']!r}")
            print(f"        {a['message']}")
        return 0

    # Stage and commit each action individually for a richer graph
    for a in actions:
        file_path = ROOT / a["file"]
        _git("add", str(file_path))
        # also stage the run log
        _git("add", "run_logs/")
        _git("commit", "--allow-empty", "-m", a["message"])
        print(f"  [commit] {a['message']}")

    _git("push", "--set-upstream", "origin", "main")
    print("[lexeng] pushed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
