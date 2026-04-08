# face

A terminal-based animated 3D talking face, synchronized to ElevenLabs TTS audio.
Renders entirely in ASCII art using a numpy-vectorised SDF ray marcher — no meshes, no assets.

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
├── core/
│   ├── renderer.py      # SDF ray marcher → numpy float32 framebuffer
│   ├── face_model.py    # Blend-shape parameters (emotions, visemes)
│   └── animator.py      # Keyframe interpolation, expression FSM
├── output/
│   └── terminal.py      # aalib / curses ASCII renderer + static layer
├── audio/
│   └── elevenlabs.py    # ElevenLabs TTS with timestamps → viseme timeline
└── main.py              # Entry point — wires everything together
```

## How it works

1. **3D face** — A ray marcher evaluates Signed Distance Functions (SDF) for each
   face primitive (head, eyes, nose, lips, brows, ears) per pixel. Blend-shape
   weights smoothly morph shapes for expression and speech in real time.

2. **Renderer** — Pure numpy; vectorised SDF evaluation + per-material Phong shading.
   Output is a float32 luminance framebuffer scaled to terminal dimensions.

3. **Terminal output** — Uses `aalib` (if installed) or falls back to a curses
   brightness-to-ASCII ramp. `SIGWINCH` handler redraws at new terminal size live.

4. **Static layer** — BB-style TV static drawn in the background. Each frame,
   background pixels are filled with characters drawn from a restricted pool of
   rotationally/reflectively symmetric alphanumerics (`0 1 8 A H I M O T U V W X Y`)
   plus non-alphanumeric symbols (`| - + = * @ # % : .`), with randomised
   curses bold/normal/dim attributes for a flickery CRT look. Intensity is tunable
   via `--static`.

5. **Audio & lipsync** — ElevenLabs `/v1/text-to-speech/{voice_id}/with-timestamps`
   returns per-character timing. Characters map to 9 standard viseme groups,
   producing a timeline of mouth blend-shape keyframes. Audio plays via
   `sounddevice` while the animator follows the timeline in a separate thread.

6. **Emotions** — Five emotion blend weights (neutral, happy, sad, surprised, angry)
   can be set via `--emotion` or driven programmatically.

7. **Intro sequence** — On startup the face emerges from static: the renderer fades
   in through a three-phase transition (pure static → static dissolve → solid face).

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
