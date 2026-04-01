# scratchpad

Experimental projects and research tools.

---

## [tv-scanner](./tv-scanner) — TV Library Episode Checker

Scans a local TV library directory and compares it against the [TVmaze](https://www.tvmaze.com/api) public API to identify missing or extra episodes. Outputs a per-show report in text, JSON, or CSV format.

**Key features:**
- Flexible season/episode filename parsing (SxxExx, 1x02, etc.)
- Parallel TVmaze lookups with polite rate limiting
- Only checks aired episodes — skips unaired/no-airdate entries
- Episode-count disambiguation when multiple TVmaze matches exist
- Skips shows marked "In Development"
- Collapses entirely-missing seasons to a single line
- Color output when terminal supports it (`--no-color` to disable)
- `--missing-only` flag to cut through the noise

---

## [face](./face) — Terminal 3D Talking Face

An animated 3D face rendered entirely in ASCII art in a terminal window,
synchronized to ElevenLabs TTS audio output.

**Key features:**
- SDF (Signed Distance Field) ray marcher — no meshes, no assets, pure numpy
- Per-material shading: eyes, brows, lips, teeth each have distinct luminance
- 9 viseme groups mapped from ElevenLabs per-character timestamps for lipsync
- 5 emotion blend shapes (neutral, happy, sad, surprised, angry)
- Idle head motion, spontaneous blinking, smooth keyframe interpolation
- Terminal resize support via `SIGWINCH` — resolution updates live
- aalib backend with curses fallback for ASCII rendering
- `--no-audio` mode requires no API key

```bash
cd face && pip install -r requirements.txt
python main.py --idle                          # idle animation
python main.py --no-audio "hello there"        # animate, no TTS
ELEVENLABS_API_KEY=sk_... python main.py "Hi" # full TTS + lipsync
```

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

---

## [voice-agent](./voice-agent) — AI Phone Call Agent

A real-time AI voice agent that handles live phone calls. Twilio streams
audio into the server, Deepgram transcribes speech in real time, Claude
generates a conversational reply, and ElevenLabs synthesises the response
back into audio — all within a single live phone call.

**Key features:**
- Full duplex: speech-in → text → LLM → TTS → audio-out in one pipeline
- Streaming TTS via ElevenLabs → ffmpeg → μ-law encoding for Twilio
- Deepgram nova-3 STT with utterance-end detection
- Outbound call trigger via `/call` HTTP endpoint
- Auto-sends full call transcript to Signal on hang-up
- Supervisor and `@reboot` cron scripts for persistent operation

```bash
cd voice-agent && npm install
bash start.sh   # starts ngrok + server, sets Twilio webhook
```
