# scratchpad

Experimental projects and research tools.

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
