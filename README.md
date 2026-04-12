# scratchpad

Experimental projects and research tools.

---

## [movie-scanner](./movie-scanner) - Movie Library Audit Tool

Scans a directory of video files to identify corruption and rank files by encoding efficiency (bits per pixel per second). Helps surface bloated or poorly encoded movies in a large library.

**Key features:**
- Dependency check with install hints (requires `ffmpeg`/`ffprobe`)
- Parallel probing across all CPU cores
- Detects corruption: unreadable files, missing video streams, zero duration
- `--deep` mode decodes frames at 10/50/90% through each file for thorough corruption detection
- Efficiency metric: bitrate ÷ pixels, colour-coded grades (excellent to terrible)
- Sortable by efficiency, size, bitrate, resolution, codec, fps, duration
- Terminal table output plus optional CSV, plain-text, and HTML export
- Recursive scan opt-in (`-r`)

```bash
python3 movie-scanner.py /mnt/movies
python3 movie-scanner.py -r /mnt/movies --deep --csv results.csv
```

---

## [tv-scanner](./tv-scanner) - TV Library Episode Checker

Scans a local TV library directory and compares it against TV episode metadata from [TVmaze](https://www.tvmaze.com/api) and/or [TheTVDB](https://thetvdb.com) to identify missing or extra episodes. Outputs a per-show report in text, JSON, CSV, or HTML format.

**Key features:**
- Flexible season and episode filename parsing
- Supports standard files like `S04E03 - Episode Name.mkv`
- Supports multi-episode files like `S01E01-E03 - Episode Name.mkv` and `S02E05E06 - Episode Name.mkv`
- Supports `tvmaze`, `thetvdb`, or `both` source modes
- In `both` mode, queries both sources and picks the one whose episode list best matches the local filesystem
- Parallel API lookups with polite rate limiting
- Only checks aired episodes, skipping unaired and no-airdate entries
- TVmaze match disambiguation based on local episode counts
- Skips TVmaze shows marked `In Development`
- Collapses entirely missing seasons to a single line in text output
- Color terminal output when supported (`--no-color` to disable)
- `--missing-only` flag to reduce noise
- Interactive self-contained HTML report with sorting, filtering, source badges, and inline missing/extra episode detail

```bash
# TVmaze only
python3 tv_scanner.py /mnt/tv

# TheTVDB only
python3 tv_scanner.py /mnt/tv --source thetvdb --thetvdb-apikey ~/.thetvdb_key

# Query both and pick the best match per show
python3 tv_scanner.py /mnt/tv --source both --thetvdb-apikey ~/.thetvdb_key

# Export HTML report
python3 tv_scanner.py /mnt/tv --source both --thetvdb-apikey ~/.thetvdb_key --output html --outfile report.html
```

---

## [face](./face) - Terminal 3D Talking Face

An animated 3D face rendered entirely in ASCII art in a terminal window,
synchronized to ElevenLabs TTS audio output.

**Key features:**
- Real OBJ mesh (`teen_head.obj`) with grid decimation to ~1 K triangles at load time
- Triangle rasteriser with z-buffer, barycentric interpolation, and Phong shading, pure numpy
- Gaussian blend-shape deformer: jaw, lips, brows, cheeks, eyes, all driven per frame
- 9 viseme groups mapped from ElevenLabs per-character timestamps for lipsync
- 5 emotion blend shapes (neutral, happy, sad, surprised, angry)
- Idle head motion, spontaneous blinking, smooth keyframe interpolation
- BB-style TV static layer: symmetric character set, randomised curses attrs
- Intro sequence: face emerges from static across a three-phase dissolve
- Terminal resize support via `SIGWINCH`, resolution updates live
- aalib backend with curses fallback for ASCII rendering
- `--demo` mode requires no API key

```bash
cd face && pip install -r requirements.txt
python main.py --demo                          # built-in demo, no API key
python main.py --idle                          # idle animation
python main.py --no-audio "hello there"        # animate, no TTS
ELEVENLABS_API_KEY=sk_... python main.py "Hi" # full TTS + lipsync
```

---

## [lexeng](./lexeng) - Lexical Entropy Engine

A Python framework for entropic analysis of lexical lattice structures.
Lexeng ingests two-dimensional word matrices and applies spectral
decomposition to derive operational schedules for downstream mutation
pipelines. The engine runs continuously, producing incremental
transformations across the codebase as the corpus evolves.

**Key components:**

- **CorpusMatrix** - row-major word lattice backed by `.mat` files; supports seeded generation, token-level mutation, and Blake2s fingerprinting
- **EntropyEngine** - derives Shannon entropy per row/column, spectral band decomposition, and normalised operational depth from a given matrix state
- **LexicalMutator** - applies weighted transforms (annotation, patch, refactor, rebalance) seeded by the current matrix fingerprint
- **TransductionPipeline** - orchestrates a full cycle: matrix seeding -> target selection -> mutation -> commit manifest

Designed for unattended operation via cron. Each run produces a structured
JSON manifest in `lexeng/run_logs/` and emits one git commit per action.
Transform weights and action frequency are tunable via `lexeng/config.json`.

---

## [voice-agent](./voice-agent) - AI Phone Call Agent

A real-time AI voice agent that handles live phone calls. Twilio streams
audio into the server, Deepgram transcribes speech in real time, Claude
generates a conversational reply, and ElevenLabs synthesises the response
back into audio, all within a single live phone call.

**Key features:**
- Full duplex: speech-in -> text -> LLM -> TTS -> audio-out in one pipeline
- Streaming TTS via ElevenLabs -> ffmpeg -> mu-law encoding for Twilio
- Deepgram nova-3 STT with utterance-end detection
- Outbound call trigger via `/call` HTTP endpoint
- Auto-sends full call transcript to Signal on hang-up
- Supervisor and `@reboot` cron scripts for persistent operation

```bash
cd voice-agent && npm install
bash start.sh   # starts ngrok + server, sets Twilio webhook
```
