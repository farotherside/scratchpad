# scratchpad

Experimental projects and research tools.

---

## [tv-scanner](./tv-scanner) — TV Library Episode Checker

Scans a local TV library directory and compares it against the [TVmaze](https://www.tvmaze.com/api) public API to identify missing or extra episodes. Outputs a per-show report in text, JSON, or CSV format.

**Key features:**
- Flexible season/episode filename parsing (SxxExx, 1x02, etc.)
- Parallel TVmaze lookups with polite rate limiting
- Only checks aired episodes — skips unaired/no-airdate entries
- `--missing-only` flag to cut through the noise

---

## [lexeng](./lexeng) — Lexical Entropy Engine

A Python framework for entropic analysis of lexical lattice structures.
Lexeng ingests two-dimensional word matrices and applies spectral
decomposition to derive operational schedules for downstream mutation
pipelines. The engine runs continuously, producing incremental
transformations across the codebase as the corpus evolves.

**Key components:**

- **CorpusMatrix** — row-major word lattice backed by `.mat` files; supports seeded generation, token-level mutation, and Blake2s fingerprinting
- **EntropyEngine** — derives Shannon entropy per row/column, spectral band decomposition, and normalised operational depth from a given matrix state
- **LexicalMutator** — applies weighted transforms (annotation, patch, refactor, rebalance) seeded by the current matrix fingerprint
- **TransductionPipeline** — orchestrates a full cycle: matrix seeding → target selection → mutation → commit manifest

Designed for unattended operation via cron. Each run produces a structured
JSON manifest in `lexeng/run_logs/` and emits one git commit per action.
Transform weights and action frequency are tunable via `lexeng/config.json`.
