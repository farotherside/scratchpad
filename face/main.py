#!/usr/bin/env python3
"""
face/main.py — Entry point for the terminal talking face.

Usage:
    python main.py [options] "Text to speak"
    python main.py --idle              # idle animation, no TTS
    python main.py --no-audio "Text"   # animate without playing audio
"""

import argparse
import os
import sys
import time
import threading
from pathlib import Path

# Ensure the face/ directory is on sys.path so absolute imports work
# whether run as `python main.py` or `python -m face.main`
sys.path.insert(0, str(Path(__file__).parent))

# ---------------------------------------------------------------------------
# Dependency check with friendly errors
# ---------------------------------------------------------------------------
def _check_deps():
    missing = []
    try:
        import numpy
    except ImportError:
        missing.append("numpy")
    if missing:
        print(f"Missing required packages: {', '.join(missing)}")
        print(f"Install with: pip install {' '.join(missing)}")
        sys.exit(1)

_check_deps()

import numpy as np

from core.face_model import FaceParams, EMOTIONS
from core.renderer import render
from core.animator import Animator, Keyframe
from output.terminal import TerminalDisplay, get_terminal_size

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args():
    parser = argparse.ArgumentParser(
        description="Terminal 3D talking face with ElevenLabs TTS"
    )
    parser.add_argument(
        "text", nargs="?", default="",
        help="Text to speak (omit for --idle mode)"
    )
    parser.add_argument(
        "--voice", default="jqcCZkN6Knx8BJ5TBdYR",
        help="ElevenLabs voice ID"
    )
    parser.add_argument(
        "--apikey", default=None,
        help="ElevenLabs API key (or set ELEVENLABS_API_KEY)"
    )
    parser.add_argument(
        "--emotion", choices=EMOTIONS, default="neutral",
        help="Starting emotion (default: neutral)"
    )
    parser.add_argument(
        "--fps", type=int, default=15,
        help="Target render FPS (default: 15)"
    )
    parser.add_argument(
        "--no-audio", action="store_true",
        help="Render animation without playing audio"
    )
    parser.add_argument(
        "--idle", action="store_true",
        help="Run idle animation loop without TTS"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help='Animate the face saying the built-in demo phrase (no API key needed)'
    )
    parser.add_argument(
        "--ramp", choices=["dense", "simple"], default="dense",
        help="ASCII brightness ramp style (default: dense)"
    )
    parser.add_argument(
        "--static", type=float, default=0.85, metavar="INTENSITY",
        help="Background static intensity 0.0=off 1.0=full (default: 0.85)"
    )
    parser.add_argument(
        "--render-width", type=int, default=None,
        help="Override render framebuffer width (default: terminal width * 2)"
    )
    parser.add_argument(
        "--render-height", type=int, default=None,
        help="Override render framebuffer height (default: terminal height * 4)"
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Demo lipsync — drives mouth shapes from text chars, no API key needed
# ---------------------------------------------------------------------------
DEMO_TEXT = "Hello, this is the face animating to show it saying something!"
_SECS_PER_SYLLABLE = 0.35   # how long each viseme shape is held
_SECS_WORD_GAP     = 0.15   # pause between words
_RAMP_TIME         = 0.08   # ramp-in / ramp-out duration


def _word_to_visemes(word: str) -> list[int]:
    """Extract one representative viseme per vowel-cluster in a word.

    Rather than iterating every character, we find vowel runs and
    representative consonant clusters so the mouth moves at syllable
    rate, not character rate.
    """
    from core.face_model import CHAR_TO_VISEME
    vowels = set("aeiou")
    result = []
    i = 0
    w = word.lower()
    while i < len(w):
        ch = w[i]
        if ch in vowels:
            # Take the vowel's viseme and skip the whole vowel run
            result.append(CHAR_TO_VISEME.get(ch, 0))
            while i < len(w) and w[i] in vowels:
                i += 1
        else:
            # Consonant: include if it has a distinct viseme (not 0)
            v = CHAR_TO_VISEME.get(ch, 0)
            if v != 0:
                result.append(v)
            i += 1
    return result or [0]


def _make_demo_keyframes(text: str, start_offset: float = 0.3) -> list[Keyframe]:
    """Generate syllable-rate lipsync keyframes from text.

    Groups text into words, maps each word to a short list of visemes
    (one per syllable / consonant cluster), and spaces them out at a
    comfortable speaking pace.
    """
    import re
    now = time.monotonic() + start_offset
    keyframes = []
    t = now

    words = re.findall(r"[a-zA-Z']+", text)
    for word in words:
        visemes = _word_to_visemes(word)
        for v in visemes:
            if v == 0:
                continue
            # Ramp up
            keyframes.append(Keyframe(t=t, viseme_index=v, viseme_weight=0.0))
            keyframes.append(Keyframe(t=t + _RAMP_TIME, viseme_index=v, viseme_weight=0.85))
            # Hold
            hold_end = t + _RAMP_TIME + _SECS_PER_SYLLABLE
            keyframes.append(Keyframe(t=hold_end, viseme_index=v, viseme_weight=0.85))
            # Ramp down
            keyframes.append(Keyframe(t=hold_end + _RAMP_TIME, viseme_index=v, viseme_weight=0.0))
            t += _RAMP_TIME + _SECS_PER_SYLLABLE + _RAMP_TIME
        # Gap between words
        t += _SECS_WORD_GAP

    # Closing rest
    keyframes.append(Keyframe(t=t + 0.3, viseme_index=0, viseme_weight=0.0))
    return keyframes


# ---------------------------------------------------------------------------
# Lipsync loader (runs in a thread so rendering continues during API call)
# ---------------------------------------------------------------------------
def _load_lipsync(
    text: str,
    voice_id: str,
    api_key: str,
    emotion: str,
    animator: Animator,
    play_audio: bool,
    ready_event: threading.Event,
    error_holder: list,
):
    """Background thread: fetch TTS, load keyframes, optionally play audio."""
    try:
        from audio.elevenlabs import synthesise, play_audio as _play

        pcm, sr, keyframes = synthesise(
            text=text,
            voice_id=voice_id,
            api_key=api_key,
            emotion=emotion,
        )

        # Offset keyframes to start slightly after now so render loop catches up
        now = time.monotonic()
        start_offset = now + 0.15   # 150ms lead time
        for kf in keyframes:
            kf.t += start_offset

        animator.load_lipsync(keyframes)
        ready_event.set()

        if play_audio:
            time.sleep(0.15)   # match the offset above
            _play(pcm, sr, blocking=True)

    except Exception as exc:
        error_holder.append(exc)
        ready_event.set()


# ---------------------------------------------------------------------------
# Main render loop
# ---------------------------------------------------------------------------
def run(args):
    from output.terminal import _RAMP_70, _RAMP_10
    ramp = _RAMP_70 if args.ramp == "dense" else _RAMP_10

    animator = Animator(emotion=args.emotion)
    frame_delay = 1.0 / max(1, args.fps)

    # Decide render resolution (oversampled vs terminal size for quality)
    # The ray marcher is expensive — terminal size is enough for the detail level
    def get_render_size(cols, rows):
        w = args.render_width  or cols
        h = args.render_height or rows
        return w, h

    lipsync_ready = threading.Event()
    lipsync_errors: list = []

    # --no-audio with text: animate mouth shapes without fetching TTS
    # --idle or no text:     pure idle animation
    # --demo:               built-in phrase, char-driven lipsync, no API needed
    demo_mode = args.demo
    idle_mode = args.idle or (not args.text and not demo_mode)
    tts_mode = not idle_mode and not demo_mode and not args.no_audio

    if demo_mode:
        # Load char-driven keyframes immediately — no API needed
        demo_text = args.text if args.text else DEMO_TEXT
        animator.load_lipsync(_make_demo_keyframes(demo_text))
        lipsync_ready.set()
    elif tts_mode:
        # Kick off lipsync + audio fetch in background
        api_key = args.apikey or os.environ.get("ELEVENLABS_API_KEY", "")
        lipsync_thread = threading.Thread(
            target=_load_lipsync,
            args=(
                args.text,
                args.voice,
                api_key,
                args.emotion,
                animator,
                True,   # play_audio
                lipsync_ready,
                lipsync_errors,
            ),
            daemon=True,
        )
        lipsync_thread.start()
    else:
        # No API call needed — mark lipsync as immediately ready
        lipsync_ready.set()

    # -----------------------------------------------------------------------
    # Render loop
    # -----------------------------------------------------------------------
    with TerminalDisplay(use_aalib=True, ramp=ramp, static=args.static) as td:
        if idle_mode:
            status = "Idle  [q to quit]"
        elif demo_mode:
            status = f"Demo  [{args.emotion}]  [q to quit]"
        elif tts_mode:
            status = "Fetching TTS…"
        else:
            status = f"Animating (no audio)  [{args.emotion}]  [q to quit]"
        running = True

        while running:
            loop_start = time.monotonic()

            # Handle input
            key = td.poll_input()
            if key in (ord("q"), ord("Q"), 27):   # q / Esc
                break

            # Update status once lipsync is ready
            if tts_mode and lipsync_ready.is_set():
                if lipsync_errors:
                    status = f"Error: {lipsync_errors[0]}"
                else:
                    status = f"Speaking  [{args.emotion}]  [q to quit]"

            # Get current animation params
            now = time.monotonic()
            params = animator.tick(now)

            # Render framebuffer
            cols, rows = td.cols, td.rows
            rw, rh = get_render_size(cols, rows)
            buf = render(rw, rh, params)

            # Show — status on last line, debug overlay on second-to-last
            debug = td.debug_line
            td.show(buf, status_line=status, debug_line=debug)

            # Check if we're done speaking (TTS + demo mode — exit after speech ends)
            if ((tts_mode or demo_mode)
                    and lipsync_ready.is_set()
                    and not lipsync_errors
                    and params.viseme_weight < 0.01):
                # Give half a second after speech ends, then exit
                if not hasattr(run, "_speech_end"):
                    run._speech_end = now
                elif now - run._speech_end > 0.5:
                    break

            # Frame rate cap
            elapsed = time.monotonic() - loop_start
            sleep_for = frame_delay - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    args = parse_args()
    try:
        run(args)
    except KeyboardInterrupt:
        pass
