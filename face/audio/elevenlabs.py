"""
elevenlabs.py — ElevenLabs TTS with character timestamps → viseme keyframe timeline.

Uses the /v1/text-to-speech/{voice_id}/with-timestamps endpoint which returns:
  - audio_base64:    the MP3/PCM audio
  - alignment:       {characters, character_start_times_seconds, character_end_times_seconds}

The lipsync pipeline:
  1. Map each character to a viseme group (CHAR_TO_VISEME)
  2. Build a timeline of Keyframes with viseme weights ramping up/down
  3. Return (pcm_bytes, sample_rate, keyframes) to the caller
"""

import base64
import io
import json
import os
import time
from typing import Optional

import requests

from core.animator import Keyframe
from core.face_model import CHAR_TO_VISEME

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ELEVENLABS_BASE = "https://api.elevenlabs.io/v1"
DEFAULT_MODEL   = "eleven_turbo_v2"
VISEME_ATTACK   = 0.025   # seconds to ramp up to full viseme weight
VISEME_RELEASE  = 0.035   # seconds to ramp down after character ends

# ---------------------------------------------------------------------------
# Audio decoding
# ---------------------------------------------------------------------------

def _decode_audio(audio_base64: str) -> tuple[bytes, int]:
    """
    Decode base64 MP3 from ElevenLabs → raw PCM (int16, mono, 22050 Hz).
    Returns (pcm_bytes, sample_rate).
    Requires: pydub  (which wraps ffmpeg/avconv)
    """
    try:
        from pydub import AudioSegment
    except ImportError:
        raise RuntimeError(
            "pydub is required for audio decoding.  "
            "Install with: pip install pydub"
        )

    mp3_bytes = base64.b64decode(audio_base64)
    audio = AudioSegment.from_file(io.BytesIO(mp3_bytes), format="mp3")
    audio = audio.set_channels(1).set_sample_width(2)   # mono, 16-bit
    sr = audio.frame_rate
    pcm = audio.raw_data
    return pcm, sr


# ---------------------------------------------------------------------------
# Lipsync keyframe builder
# ---------------------------------------------------------------------------

def _build_keyframes(
    chars: list[str],
    starts: list[float],
    ends: list[float],
    time_offset: float = 0.0,
    emotion: str = "neutral",
) -> list[Keyframe]:
    """
    Convert character alignment data into a list of Keyframes.

    Strategy:
    - For each character, emit a ramp-up keyframe at char_start - ATTACK
      and a ramp-down keyframe at char_end.
    - Silences get a rest (viseme 0, weight 0) keyframe.
    - Consecutive characters in the same viseme group are merged into one
      held pose to reduce jitter.
    """
    keyframes: list[Keyframe] = []

    def _kf(t: float, v_idx: int, v_w: float) -> Keyframe:
        return Keyframe(
            t=t + time_offset,
            viseme_index=v_idx,
            viseme_weight=v_w,
            emotion_a=emotion,
            emotion_b=emotion,
            emotion_blend=0.0,
        )

    prev_group = -1

    for ch, start, end in zip(chars, starts, ends):
        group = CHAR_TO_VISEME.get(ch.lower(), 0)
        duration = end - start

        if group == 0 or duration < 0.01:
            # Silence or very short — fade to rest
            if prev_group != 0:
                keyframes.append(_kf(start, 0, 0.0))
            prev_group = 0
            continue

        # Ramp up
        ramp_up_t = max(start - VISEME_ATTACK, start)
        if prev_group != group:
            keyframes.append(_kf(ramp_up_t, group, 0.0))

        # Hold (peak at midpoint)
        mid_t = (start + end) / 2
        keyframes.append(_kf(mid_t, group, 1.0))

        # Ramp down
        keyframes.append(_kf(end, group, 0.8))
        keyframes.append(_kf(end + VISEME_RELEASE, 0, 0.0))

        prev_group = group

    # Final rest keyframe
    if ends:
        keyframes.append(_kf(ends[-1] + 0.3, 0, 0.0))

    # Sort by time and deduplicate adjacent identical entries
    keyframes.sort(key=lambda k: k.t)
    return keyframes


# ---------------------------------------------------------------------------
# ElevenLabs TTS call
# ---------------------------------------------------------------------------

def synthesise(
    text: str,
    voice_id: str,
    api_key: Optional[str] = None,
    model_id: str = DEFAULT_MODEL,
    emotion: str = "neutral",
    stability: float = 0.45,
    similarity_boost: float = 0.80,
    style: float = 0.20,
) -> tuple[bytes, int, list[Keyframe]]:
    """
    Call ElevenLabs with-timestamps endpoint.

    Returns:
        pcm_bytes   — raw 16-bit mono PCM audio
        sample_rate — audio sample rate in Hz
        keyframes   — lipsync Keyframe list (times relative to playback start = 0)
    """
    key = api_key or os.environ.get("ELEVENLABS_API_KEY", "")
    if not key:
        raise ValueError(
            "ElevenLabs API key required.  Pass --apikey or set ELEVENLABS_API_KEY."
        )

    url = f"{ELEVENLABS_BASE}/text-to-speech/{voice_id}/with-timestamps"
    headers = {
        "xi-api-key": key,
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": True,
        },
        "output_format": "mp3_44100_128",
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    audio_b64   = data["audio_base64"]
    alignment   = data.get("alignment", {})
    chars       = alignment.get("characters", [])
    starts      = alignment.get("character_start_times_seconds", [])
    ends        = alignment.get("character_end_times_seconds", [])

    pcm, sr = _decode_audio(audio_b64)
    keyframes = _build_keyframes(chars, starts, ends, emotion=emotion)

    return pcm, sr, keyframes


# ---------------------------------------------------------------------------
# Audio playback
# ---------------------------------------------------------------------------

def play_audio(pcm: bytes, sample_rate: int, blocking: bool = False):
    """
    Play PCM audio via sounddevice.
    Returns immediately if blocking=False (audio plays in background stream).
    """
    try:
        import sounddevice as sd
        import numpy as np
    except ImportError:
        raise RuntimeError(
            "sounddevice is required for audio playback.  "
            "Install with: pip install sounddevice"
        )

    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    if blocking:
        sd.play(samples, samplerate=sample_rate)
        sd.wait()
    else:
        sd.play(samples, samplerate=sample_rate)
