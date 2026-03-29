# lexeng — Lexical Entropy Engine

A lightweight Python framework for entropic transduction of lexical
lattice structures. The engine consumes two-dimensional word matrices
(`.mat` files) and applies configurable spectral decomposition passes
to derive operational schedules for downstream mutation pipelines.

## Architecture

```
corpus/          ← word matrix files (.mat)
lexeng/
  corpus.py      ← CorpusMatrix: lattice I/O + seeding
  engine.py      ← EntropyEngine: spectral analysis
  mutator.py     ← LexicalMutator: weighted transforms
  pipeline.py    ← TransductionPipeline: full cycle orchestration
run.py           ← CLI entry point
config.json      ← tunable runtime parameters
tools/
  gen_corpus.py  ← regenerate corpus matrices
run_logs/        ← per-run JSON manifests (auto-created)
```

## Quickstart

```bash
# Generate corpus matrices (requires /usr/share/dict/words or similar)
python tools/gen_corpus.py --count 4

# Run one transduction cycle (dry run — no git ops)
python run.py --dry-run

# Full run with custom config
python run.py --config config.json
```

## Configuration (`config.json`)

| Key | Default | Description |
|---|---|---|
| `min_actions` | 1 | Minimum mutations per run |
| `max_actions` | 6 | Maximum mutations per run |
| `weights.annotate` | 0.35 | Probability weight for annotation passes |
| `weights.patch` | 0.30 | Probability weight for patch operations |
| `weights.refactor` | 0.20 | Probability weight for refactor passes |
| `weights.rebalance` | 0.15 | Probability weight for rebalance operations |
| `rotate_corpus` | true | Mutate corpus between actions |
| `corpus_mutation_depth` | 3 | Tokens mutated per corpus rotation |

## Cron Setup

```cron
# Run daily at a randomised time (e.g. between 09:00–17:00)
0 9 * * * cd /path/to/lexeng && python run.py --config config.json >> run.log 2>&1
```

For a more natural commit distribution, wrap in a short random sleep:

```bash
#!/bin/bash
sleep $((RANDOM % 28800))   # up to 8h of drift
cd /path/to/lexeng
python run.py --config config.json
```
