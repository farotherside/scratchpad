# face

A terminal-based animated 3D talking face, synchronized to ElevenLabs TTS audio.
Renders a real OBJ mesh (`teen_head.obj`) as ASCII art using a numpy-vectorised
triangle rasteriser with z-buffering and Phong shading — no external 3D libraries.

## Demo

```bash
cd face && pip install -r requirements.txt
python main.py --demo                          # built-in phrase, no API key
python main.py --idle                          # idle animation loop
python main.py --no-audio "hello there"        # animate without TTS
ELEVENLABS_API_KEY=sk_... python main.py "Hi!" # full TTS + lipsync
```

## Architecture

```
face/
├── assets/
│   └── teen_head.obj    # Source mesh (~16 K triangles, decimated at load time)
├── core/
│   ├── mesh.py          # OBJ loader, mesh decimation, blend-shape deformer
│   ├── renderer.py      # Triangle rasteriser → numpy float32 framebuffer
│   ├── face_model.py    # Blend-shape parameters (emotions, visemes)
│   └── animator.py      # Keyframe interpolation, expression FSM
├── output/
│   └── terminal.py      # aalib / curses ASCII renderer + static layer
├── audio/
│   └── elevenlabs.py    # ElevenLabs TTS with timestamps → viseme timeline
└── main.py              # Entry point — wires everything together
```

## How it works

1. **Mesh** — `teen_head.obj` is loaded once at startup. The raw ~16 K triangle
   mesh is grid-decimated to ~1 K triangles for real-time performance. Vertices
   are centred and normalised so the head spans roughly ±1 in all axes.

2. **Blend shapes** — Per-vertex Gaussian influence fields drive facial deformation:
   jaw drop, mouth open/wide, smile, frown, brow raise/furrow, cheek raise,
   eye wide/squint. Weights are interpolated each frame from the animator.

3. **Renderer** — Pure numpy pipeline per frame:
   - Apply blend-shape deformation to vertex positions
   - Apply head-pose rotation (yaw / pitch / roll)
   - Perspective-project vertices to screen space with character-aspect correction
   - Backface-cull and screen-bbox filter (vectorised)
   - Per-triangle rasterise with barycentric interpolation + z-buffer
   - Phong shade hit pixels (two lights + ambient + specular)
   Output is a float32 luminance framebuffer scaled to terminal dimensions.

4. **Terminal output** — Uses `aalib` (if installed) or falls back to a curses
   brightness-to-ASCII ramp. `SIGWINCH` handler redraws at new terminal size live.

5. **Static layer** — BB-style TV static drawn in the background. Characters are
   drawn from a restricted pool of rotationally/reflectively symmetric
   alphanumerics (`0 1 8 A H I M O T U V W X Y`) plus non-alphanumeric symbols
   (`| - + = * @ # % : .`), with randomised curses bold/normal/dim attributes
   for a flickery CRT look. Intensity tunable via `--static`.

6. **Audio & lipsync** — ElevenLabs `/v1/text-to-speech/{voice_id}/with-timestamps`
   returns per-character timing. Characters map to 9 standard viseme groups,
   producing a timeline of mouth blend-shape keyframes. Audio plays via
   `sounddevice` while the animator follows the timeline in a separate thread.

7. **Emotions** — Five emotion blend weights (neutral, happy, sad, surprised, angry)
   can be set via `--emotion` or driven programmatically.

8. **Intro sequence** — On startup the face emerges from static across a
   three-phase dissolve (pure static → static dissolve → solid face).

## Requirements

```
pip install numpy sounddevice requests
# Optional for enhanced ASCII art:
pip install aalib Pillow   # or: apt install python3-aalib
```

## Options

```
python main.py [OPTIONS] [TEXT]

Positional:
  text                   Text to speak (omit for --idle / --demo)

Flags:
  --idle                 Run idle animation loop (no TTS)
  --demo                 Animate the built-in demo phrase (no API key needed)
  --no-audio             Render animation without playing audio

TTS:
  --voice VOICE_ID       ElevenLabs voice ID (default: built-in)
  --apikey KEY           ElevenLabs API key (or set ELEVENLABS_API_KEY env var)
  --emotion EMOTION      Starting emotion: neutral|happy|sad|surprised|angry

Rendering:
  --fps N                Target render FPS (default: 15)
  --static INTENSITY     Background static 0.0=off 1.0=full (default: 0.85)
  --ramp dense|simple    ASCII brightness ramp style (default: dense)
  --render-width N       Override framebuffer width (default: terminal width × 2)
  --render-height N      Override framebuffer height (default: terminal height × 4)
```
