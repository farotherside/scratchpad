# face

A terminal-based animated 3D talking face, synchronized to ElevenLabs TTS audio.

## Architecture

```
face/
├── core/
│   ├── renderer.py      # SDF ray marcher → numpy float32 framebuffer
│   ├── face_model.py    # Blend-shape parameters (emotions, visemes)
│   └── animator.py      # Keyframe interpolation, expression FSM
├── output/
│   └── terminal.py      # aalib / curses ASCII renderer + SIGWINCH resize
├── audio/
│   └── elevenlabs.py    # ElevenLabs TTS with timestamps → viseme timeline
└── main.py              # Entry point — wires everything together
```

## How it works

1. **3D Face** — A raymarcher evaluates a set of Signed Distance Functions (SDF)
   describing the face geometry (head sphere, eyes, nose, lips) at every pixel.
   Blend-shape weights smoothly morph lip/brow/cheek shapes for expression and speech.

2. **Renderer** — Pure numpy; vectorised per-pixel SDF + Phong shading.
   Output is a float32 luminance framebuffer scaled to terminal dimensions.

3. **Terminal output** — Uses `aalib` (if installed) or falls back to a curses
   brightness-to-ASCII ramp. Responds to `SIGWINCH` to redraw at new terminal size.

4. **Audio & lipsync** — ElevenLabs `/v1/text-to-speech/{voice_id}/with-timestamps`
   returns per-character timing. Characters are mapped to standard viseme groups,
   producing a timeline of mouth blend-shape keyframes. Audio is played via
   `sounddevice` while the animator follows the timeline in a separate thread.

5. **Emotions** — Separate emotion blend weights (neutral, happy, sad, surprised,
   angry) can be driven by the conversation layer or set manually.

## Requirements

```
pip install numpy sounddevice requests
# Optional for enhanced ASCII art:
pip install aalib   # or: apt install python3-aalib
```

## Usage

```bash
python main.py --voice jqcCZkN6Knx8BJ5TBdYR "Hello, I am your terminal assistant."
```

### Options

```
--voice VOICE_ID       ElevenLabs voice ID
--apikey KEY           ElevenLabs API key (or set ELEVENLABS_API_KEY env var)
--emotion EMOTION      Starting emotion: neutral|happy|sad|surprised|angry
--fps N                Target render FPS (default: 15)
--no-audio             Render animation without playing audio
--idle                 Run idle animation loop (no TTS)
```
