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
        "--ramp", choices=["dense", "simple"], default="dense",
        help="ASCII brightness ramp style (default: dense)"
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

    idle_mode = args.idle or not args.text

    if not idle_mode:
        # Kick off lipsync fetch in background
        api_key = args.apikey or os.environ.get("ELEVENLABS_API_KEY", "")
        lipsync_thread = threading.Thread(
            target=_load_lipsync,
            args=(
                args.text,
                args.voice,
                api_key,
                args.emotion,
                animator,
                not args.no_audio,
                lipsync_ready,
                lipsync_errors,
            ),
            daemon=True,
        )
        lipsync_thread.start()

    # -----------------------------------------------------------------------
    # Render loop
    # -----------------------------------------------------------------------
    with TerminalDisplay(use_aalib=True, ramp=ramp) as td:
        status = "Fetching TTS…" if not idle_mode else "Idle  [q to quit]"
        running = True

        while running:
            loop_start = time.monotonic()

            # Handle input
            key = td.poll_input()
            if key in (ord("q"), ord("Q"), 27):   # q / Esc
                break

            # Update status once lipsync is ready
            if not idle_mode and lipsync_ready.is_set():
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

            # Show
            td.show(buf, status_line=status)

            # Check if we're done speaking (non-idle, lipsync finished, viseme settled)
            if (not idle_mode
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
