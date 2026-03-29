#!/usr/bin/env python3
"""
tools/gen_corpus.py — Regenerate corpus .mat files from a word list.

Reads /usr/share/dict/words (or a custom word list) and writes
N matrix files of 16×16 five-letter tokens into corpus/.

Usage
-----
    python tools/gen_corpus.py [--count 4] [--wordlist /path/to/words]
"""

import argparse
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).parent.parent.resolve()
CORPUS_DIR = ROOT / "corpus"


def _load_words(path: str) -> list:
    words = []
    with open(path) as fh:
        for line in fh:
            w = line.strip().lower()
            if len(w) == 5 and w.isalpha():
                words.append(w)
    return words


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate lexeng corpus matrices")
    parser.add_argument("--count", type=int, default=4, help="Number of .mat files")
    parser.add_argument("--rows", type=int, default=16)
    parser.add_argument("--cols", type=int, default=16)
    parser.add_argument("--wordlist", default="/usr/share/dict/words")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    if not pathlib.Path(args.wordlist).exists():
        print(f"[gen_corpus] wordlist not found: {args.wordlist}", file=sys.stderr)
        print("[gen_corpus] falling back to synthetic tokens", file=sys.stderr)
        import itertools, string
        words = [
            "".join(c)
            for c in itertools.islice(
                itertools.product(string.ascii_lowercase, repeat=5), 4096
            )
        ]
    else:
        words = _load_words(args.wordlist)

    if not words:
        print("[gen_corpus] no valid five-letter words found", file=sys.stderr)
        return 1

    print(f"[gen_corpus] pool size: {len(words)}")
    CORPUS_DIR.mkdir(exist_ok=True)
    rng = random.Random(args.seed)

    for i in range(args.count):
        name = CORPUS_DIR / f"matrix_{i:02d}.mat"
        with open(name, "w") as fh:
            for _ in range(args.rows):
                fh.write(" ".join(rng.choice(words) for _ in range(args.cols)) + "\n")
        print(f"[gen_corpus] wrote {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
