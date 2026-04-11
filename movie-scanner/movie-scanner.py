#!/usr/bin/env python3
"""
movie-scanner.py — Movie library audit tool
Scans a directory for video files, identifies corruption, and ranks files
by codec-normalised encoding efficiency (bitrate per pixel per second,
adjusted for codec generation). Grades are percentile-based relative to
your library — not hardcoded absolute thresholds.

Usage:
    movie-scanner.py [OPTIONS] <directory>

Options:
    -r, --recursive         Recurse into subdirectories
    --deep                  Deep corruption scan (decodes frames, slower)
    --sort <field>          Sort by: efficiency, size, bitrate, resolution,
                            codec, name, fps, duration  (default: efficiency)
    --desc                  Sort descending (default: ascending)
    --csv <file>            Export results to CSV
    --txt <file>            Export results to plain text
    --no-table              Suppress terminal table output
    -j, --jobs <n>          Parallel probe jobs (default: CPU count)
    -q, --quiet             Suppress progress output
    -h, --help              Show this help
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Python version check (3.10+ for match/case, but we only need 3.8+)
# ---------------------------------------------------------------------------
if sys.version_info < (3, 8):
    print("movie-scanner requires Python 3.8 or newer.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Codec efficiency multipliers (relative to H.264 = 1.0)
# These represent how much more efficient each codec is per bit.
# An HEVC file at 1 Mbps delivers ~1.8x the quality of H.264 at 1 Mbps,
# so we divide its effective bitrate by 1.8 before scoring.
# Sources: JVET, Netflix research, industry benchmarks.
# ---------------------------------------------------------------------------
CODEC_EFFICIENCY: Dict[str, float] = {
    # Legacy / low-efficiency
    "mpeg1video":  0.3,
    "mpeg2video":  0.4,
    "vc1":         0.5,
    "wmv1":        0.4,
    "wmv2":        0.45,
    "wmv3":        0.5,
    "rv40":        0.5,   # RealVideo 4
    "rv30":        0.4,
    "rv20":        0.35,
    "flv1":        0.4,   # Sorenson Spark
    "svq3":        0.4,   # Sorenson Video 3
    "h261":        0.2,
    "h263":        0.35,
    "h263p":       0.4,
    "mjpeg":       0.25,  # Motion JPEG — very inefficient
    "dvvideo":     0.2,

    # Baseline generation (MPEG-4 ASP)
    "mpeg4":       0.7,   # Xvid / DivX
    "msmpeg4v3":   0.6,   # DivX 3
    "msmpeg4v2":   0.55,
    "msmpeg4v1":   0.5,

    # H.264 / AVC — the baseline (1.0)
    "h264":        1.0,
    "avc":         1.0,

    # VP8 — roughly on par with H.264
    "vp8":         0.9,

    # VP9 — ~40% better than H.264
    "vp9":         1.4,

    # H.265 / HEVC — ~60-80% better than H.264
    "hevc":        1.8,
    "h265":        1.8,

    # AV1 — ~50% better than HEVC, ~2.5x H.264
    "av1":         2.5,
    "libaom-av1":  2.5,

    # Lossless / archival (treat as very inefficient for screening purposes)
    "ffv1":        0.15,
    "huffyuv":     0.1,
    "utvideo":     0.1,
    "rawvideo":    0.05,
    "v210":        0.1,
    "prores":      0.3,   # ProRes — high quality but high bitrate by design
    "dnxhd":       0.3,
    "dnxhr":       0.3,
    "cineform":    0.3,
}

DEFAULT_CODEC_EFFICIENCY = 0.8   # conservative fallback for unknown codecs

# ---------------------------------------------------------------------------
# Known video extensions
# ---------------------------------------------------------------------------
VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".3gpp", ".3gpp2",
    ".asf", ".avi",
    ".dav", ".dv", ".dvr-ms",
    ".f4v", ".flv",
    ".h264", ".h265", ".hevc",
    ".m1v", ".m2t", ".m2ts", ".m2v", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".mxf",
    ".nuv",
    ".ogm", ".ogv",
    ".qt",
    ".rm", ".rmvb",
    ".ts",
    ".vob",
    ".webm", ".wmv", ".wtv",
    ".xvid",
    ".y4m",
}

# ---------------------------------------------------------------------------
# ANSI colour helpers (degrade gracefully if not a TTY)
# ---------------------------------------------------------------------------
_COLOUR = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text

RED    = lambda t: _c("91", t)
YELLOW = lambda t: _c("93", t)
GREEN  = lambda t: _c("92", t)
CYAN   = lambda t: _c("96", t)
BOLD   = lambda t: _c("1",  t)
DIM    = lambda t: _c("2",  t)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class VideoFile:
    path: Path
    size_bytes: int = 0

    # populated by probe
    probe_ok: bool = False
    corrupt: bool = False
    corrupt_reason: str = ""

    codec: str = ""
    width: int = 0
    height: int = 0
    duration_s: float = 0.0
    bitrate_bps: float = 0.0
    fps: float = 0.0
    format_name: str = ""
    audio_codecs: List[str] = field(default_factory=list)

    # computed
    codec_factor: float = 1.0       # efficiency multiplier for this codec
    raw_efficiency: float = 0.0     # bps / pixels (raw, no codec adjustment)
    norm_efficiency: float = 0.0    # raw_efficiency / codec_factor (H.264-equivalent)
    percentile: float = 0.0         # 0–100, position in library (set post-scan)
    grade: str = ""                 # excellent/good/fair/poor/terrible (set post-scan)

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 ** 2)

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "unknown"

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def duration_hms(self) -> str:
        if self.duration_s <= 0:
            return "unknown"
        h = int(self.duration_s // 3600)
        m = int((self.duration_s % 3600) // 60)
        s = int(self.duration_s % 60)
        return f"{h}:{m:02d}:{s:02d}"

    def compute_efficiency(self):
        self.codec_factor = CODEC_EFFICIENCY.get(
            self.codec.lower(), DEFAULT_CODEC_EFFICIENCY
        )
        if self.pixels > 0 and self.bitrate_bps > 0:
            self.raw_efficiency = self.bitrate_bps / self.pixels
            # Normalise: divide by codec factor → H.264-equivalent bpp
            # Lower norm_efficiency = more efficient relative to H.264 baseline
            self.norm_efficiency = self.raw_efficiency / self.codec_factor
        else:
            self.raw_efficiency  = float("inf")
            self.norm_efficiency = float("inf")


# ---------------------------------------------------------------------------
# Percentile grading
# Rank files relative to each other so grades spread across the library.
# Grade bands (percentile of norm_efficiency, lower eff = better):
#   excellent  → bottom 20%  (best-encoded fifth)
#   good       → 20–45%
#   fair       → 45–70%
#   poor       → 70–88%
#   terrible   → top 12%     (worst-encoded)
# ---------------------------------------------------------------------------
GRADE_BANDS: List[Tuple[float, str]] = [
    (20.0,  "excellent"),
    (45.0,  "good"),
    (70.0,  "fair"),
    (88.0,  "poor"),
    (100.0, "terrible"),
]

def assign_grades(files: List["VideoFile"]):
    """
    Compute percentile rank and grade for each file based on norm_efficiency.
    Files with inf efficiency (unknown bitrate) are placed in the 'terrible'
    bucket but listed separately so they don't distort the curve.
    """
    scoreable = [vf for vf in files if vf.norm_efficiency < float("inf")]
    inf_files = [vf for vf in files if vf.norm_efficiency == float("inf")]

    if not scoreable:
        for vf in inf_files:
            vf.percentile = 100.0
            vf.grade = "?"
        return

    # Sort by norm_efficiency ascending (lower = better)
    ranked = sorted(scoreable, key=lambda v: v.norm_efficiency)
    n = len(ranked)

    for i, vf in enumerate(ranked):
        # Percentile: what fraction of the library is AT OR BELOW this efficiency
        # (i.e. worse than or equal to this file)
        # We use (i+1)/n * 100 so the worst file = 100th percentile
        vf.percentile = (i + 1) / n * 100.0
        for threshold, label in GRADE_BANDS:
            if vf.percentile <= threshold:
                vf.grade = label
                break

    for vf in inf_files:
        vf.percentile = 100.0
        vf.grade = "?"


# ---------------------------------------------------------------------------
# Dependency check
# ---------------------------------------------------------------------------
def check_dependencies():
    missing = []
    for tool in ("ffprobe", "ffmpeg"):
        if not shutil.which(tool):
            missing.append(tool)
    if missing:
        print(RED(f"✗ Missing required tools: {', '.join(missing)}"))
        print(DIM("  Install via your package manager, e.g.:"))
        print(DIM("    sudo apt install ffmpeg     # Debian/Ubuntu"))
        print(DIM("    sudo dnf install ffmpeg     # Fedora"))
        print(DIM("    sudo pacman -S ffmpeg       # Arch"))
        print(DIM("    brew install ffmpeg         # macOS"))
        sys.exit(1)
    print(GREEN("✓") + f" ffprobe: {shutil.which('ffprobe')}")


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------
def collect_files(directory: Path, recursive: bool) -> List[Path]:
    results = []
    it = directory.rglob("*") if recursive else directory.iterdir()
    for p in it:
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            results.append(p)
    return sorted(results)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def run_ffprobe(path: Path) -> Optional[dict]:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def probe_file(path: Path) -> "VideoFile":
    vf = VideoFile(path=path, size_bytes=path.stat().st_size)
    data = run_ffprobe(path)

    if data is None:
        vf.corrupt = True
        vf.corrupt_reason = "ffprobe failed to read file"
        return vf

    vf.probe_ok = True

    fmt = data.get("format", {})
    vf.format_name = fmt.get("format_long_name", fmt.get("format_name", ""))
    try:
        vf.duration_s = float(fmt.get("duration", 0))
    except (ValueError, TypeError):
        vf.duration_s = 0.0
    try:
        vf.bitrate_bps = float(fmt.get("bit_rate", 0))
    except (ValueError, TypeError):
        vf.bitrate_bps = 0.0

    streams = data.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    vf.audio_codecs = [s.get("codec_name", "") for s in audio_streams]

    if not video_streams:
        vf.corrupt = True
        vf.corrupt_reason = "no video stream found"
        return vf

    vs = video_streams[0]
    vf.codec = vs.get("codec_name", "unknown")
    vf.width  = int(vs.get("width",  0))
    vf.height = int(vs.get("height", 0))

    r_fps = vs.get("r_frame_rate", "0/1")
    try:
        num, den = r_fps.split("/")
        vf.fps = float(num) / float(den) if float(den) != 0 else 0.0
    except (ValueError, ZeroDivisionError):
        vf.fps = 0.0

    if vf.bitrate_bps == 0:
        try:
            vf.bitrate_bps = float(vs.get("bit_rate", 0))
        except (ValueError, TypeError):
            pass

    if vf.duration_s <= 0:
        vf.corrupt = True
        vf.corrupt_reason = "duration is zero or missing"
        return vf

    if vf.width == 0 or vf.height == 0:
        vf.corrupt = True
        vf.corrupt_reason = "zero-dimension video stream"
        return vf

    vf.compute_efficiency()
    return vf


def deep_probe(vf: "VideoFile") -> "VideoFile":
    """Decode frames at 10/50/90% through the file to catch hidden corruption."""
    if vf.corrupt or not vf.probe_ok or vf.duration_s <= 0:
        return vf

    for frac in (0.10, 0.50, 0.90):
        seek_s = vf.duration_s * frac
        cmd = [
            "ffmpeg", "-ss", str(seek_s),
            "-i", str(vf.path),
            "-frames:v", "1", "-f", "null", "-",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if "frame=" not in result.stderr and result.returncode != 0:
                vf.corrupt = True
                vf.corrupt_reason = (
                    f"frame decode failed at {frac*100:.0f}% ({seek_s:.1f}s)"
                )
                return vf
        except subprocess.TimeoutExpired:
            vf.corrupt = True
            vf.corrupt_reason = f"decode timed out at {frac*100:.0f}%"
            return vf
        except OSError as e:
            vf.corrupt = True
            vf.corrupt_reason = f"ffmpeg error: {e}"
            return vf

    return vf


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------
def fmt_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024**3:.2f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024**2:.1f} MB"
    return f"{size_bytes / 1024:.0f} KB"


def fmt_efficiency(eff: float) -> str:
    return "N/A" if eff == float("inf") else f"{eff:.4f}"


def fmt_bitrate(bps: float) -> str:
    if bps <= 0:
        return "N/A"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.1f} Mbps"
    if bps >= 1_000:
        return f"{bps/1_000:.0f} Kbps"
    return f"{bps:.0f} bps"


def fmt_codec_factor(factor: float) -> str:
    return f"{factor:.1f}x"


GRADE_COLOUR = {
    "excellent": GREEN,
    "good":      GREEN,
    "fair":      YELLOW,
    "poor":      RED,
    "terrible":  RED,
    "?":         DIM,
}

def coloured_grade(grade: str) -> str:
    fn = GRADE_COLOUR.get(grade, lambda t: t)
    return fn(grade)

def coloured_norm_eff(vf: "VideoFile") -> str:
    txt = fmt_efficiency(vf.norm_efficiency)
    fn = GRADE_COLOUR.get(vf.grade, lambda t: t)
    return fn(txt)


# ---------------------------------------------------------------------------
# Terminal table
# ---------------------------------------------------------------------------
COLUMNS = [
    # (header, fixed_width, align)
    ("NormEff",   8, "right"),   # codec-normalised efficiency
    ("RawEff",    8, "right"),   # raw bits/pixel
    ("Codec",     7, "left"),
    ("Factor",    7, "right"),   # codec efficiency multiplier
    ("Grade",     9, "left"),
    ("Pctile",    7, "right"),   # percentile rank
    ("Size",      9, "right"),
    ("Res",      11, "left"),
    ("Bitrate",  10, "right"),
    ("Duration",  9, "right"),
    ("Filename",  0, "left"),    # fills remaining width
]

def _trunc(s: str, width: int) -> str:
    if width == 0 or len(s) <= width:
        return s
    return s[:width - 1] + "…"


def print_table(files: List["VideoFile"], sort_by: str, descending: bool):
    if not files:
        print(DIM("  (no valid files)"))
        return

    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 140

    fixed_total = sum(w for _, w, _ in COLUMNS if w > 0)
    separators  = len(COLUMNS) - 1
    filename_width = max(20, term_width - fixed_total - separators - 2)
    widths = [w if w > 0 else filename_width for _, w, _ in COLUMNS]

    # Header
    headers = [col[0] for col in COLUMNS]
    header_cells = [
        h.ljust(widths[i]) if COLUMNS[i][2] == "left" else h.rjust(widths[i])
        for i, h in enumerate(headers)
    ]
    separator = "─" * (sum(widths) + separators)

    print()
    print(BOLD(" ".join(header_cells)))
    print(DIM(separator))

    for vf in files:
        raw_cells = [
            fmt_efficiency(vf.norm_efficiency),
            fmt_efficiency(vf.raw_efficiency),
            vf.codec or "unknown",
            fmt_codec_factor(vf.codec_factor),
            vf.grade,
            f"{vf.percentile:.0f}%",
            fmt_size(vf.size_bytes),
            vf.resolution,
            fmt_bitrate(vf.bitrate_bps),
            vf.duration_hms,
            vf.path.name,
        ]

        out_cells = []
        for i, cell in enumerate(raw_cells):
            w = widths[i]
            _, _, align = COLUMNS[i]
            truncated = _trunc(cell, w)
            visible_len = len(truncated)
            padding = w - visible_len

            # Apply colour
            if i == 0:
                rendered = coloured_norm_eff(vf) if truncated == cell else coloured_norm_eff(vf)
                # re-truncate the plain text, recolour
                fn = GRADE_COLOUR.get(vf.grade, lambda t: t)
                rendered = fn(truncated)
            elif i == 4:  # Grade
                rendered = coloured_grade(truncated)
            else:
                rendered = truncated

            if align == "right":
                out_cells.append(" " * padding + rendered)
            else:
                out_cells.append(rendered + " " * padding)

        print(" ".join(out_cells))

    print(DIM(separator))
    print(f"  {len(files)} files  |  sort: {sort_by} {'↓' if descending else '↑'}  "
          f"|  NormEff = codec-normalised bits/pixel (H.264 baseline)")


def print_corrupt_section(corrupt: List["VideoFile"]):
    if not corrupt:
        return
    print()
    print(RED(BOLD(f"⚠  Corrupt / Unreadable Files ({len(corrupt)})")))
    print(DIM("─" * 60))
    for vf in corrupt:
        print(f"  {RED('✗')} {vf.path.name}")
        print(DIM(f"       Path:   {vf.path}"))
        print(DIM(f"       Reason: {vf.corrupt_reason}"))
        print(DIM(f"       Size:   {fmt_size(vf.size_bytes)}"))
    print()


# ---------------------------------------------------------------------------
# CSV / TXT export
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "path", "filename", "size_bytes", "size_mb",
    "codec", "codec_factor",
    "width", "height", "resolution",
    "duration_s", "duration_hms", "fps",
    "bitrate_bps", "bitrate_fmt",
    "raw_efficiency", "norm_efficiency",
    "percentile", "grade",
    "format_name", "audio_codecs",
    "corrupt", "corrupt_reason",
]

def file_to_dict(vf: "VideoFile") -> dict:
    return {
        "path":            str(vf.path),
        "filename":        vf.path.name,
        "size_bytes":      vf.size_bytes,
        "size_mb":         f"{vf.size_mb:.2f}",
        "codec":           vf.codec,
        "codec_factor":    f"{vf.codec_factor:.2f}",
        "width":           vf.width,
        "height":          vf.height,
        "resolution":      vf.resolution,
        "duration_s":      f"{vf.duration_s:.2f}",
        "duration_hms":    vf.duration_hms,
        "fps":             f"{vf.fps:.3f}",
        "bitrate_bps":     f"{vf.bitrate_bps:.0f}",
        "bitrate_fmt":     fmt_bitrate(vf.bitrate_bps),
        "raw_efficiency":  fmt_efficiency(vf.raw_efficiency),
        "norm_efficiency": fmt_efficiency(vf.norm_efficiency),
        "percentile":      f"{vf.percentile:.1f}",
        "grade":           vf.grade,
        "format_name":     vf.format_name,
        "audio_codecs":    ", ".join(vf.audio_codecs),
        "corrupt":         "yes" if vf.corrupt else "no",
        "corrupt_reason":  vf.corrupt_reason,
    }


def export_csv(all_files: List["VideoFile"], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for vf in all_files:
            writer.writerow(file_to_dict(vf))
    print(GREEN(f"✓ CSV exported: {path}"))


def export_txt(good: List["VideoFile"], corrupt: List["VideoFile"],
               path: str, sort_by: str, descending: bool):
    with open(path, "w", encoding="utf-8") as f:
        f.write("MOVIE-SCANNER REPORT\n")
        f.write("=" * 100 + "\n\n")
        f.write(f"Valid files ({len(good)}) — sorted by {sort_by} "
                f"({'desc' if descending else 'asc'})\n")
        f.write("NormEff = codec-normalised efficiency (H.264 baseline). "
                "Lower is more efficient.\n")
        f.write("Grade   = percentile rank within this library.\n\n")
        f.write("-" * 100 + "\n")

        col_w = [8, 8, 7, 7, 9, 7, 9, 12, 10, 9]
        headers = ["NormEff", "RawEff", "Codec", "Factor", "Grade",
                   "Pctile", "Size", "Resolution", "Bitrate", "Duration", "Filename"]
        f.write("  ".join(
            h.rjust(col_w[i]) if i < len(col_w) else h
            for i, h in enumerate(headers)
        ) + "\n")
        f.write("-" * 100 + "\n")

        for vf in good:
            d = file_to_dict(vf)
            cells = [
                d["norm_efficiency"].rjust(8),
                d["raw_efficiency"].rjust(8),
                d["codec"].ljust(7),
                d["codec_factor"].rjust(7),
                d["grade"].ljust(9),
                (d["percentile"] + "%").rjust(7),
                (d["size_mb"] + " MB").rjust(9),
                d["resolution"].ljust(12),
                d["bitrate_fmt"].rjust(10),
                d["duration_hms"].rjust(9),
                d["filename"],
            ]
            f.write("  ".join(cells) + "\n")

        if corrupt:
            f.write("\n" + "=" * 100 + "\n")
            f.write(f"CORRUPT / UNREADABLE FILES ({len(corrupt)})\n")
            f.write("-" * 100 + "\n")
            for vf in corrupt:
                f.write(f"  [CORRUPT] {vf.path}\n")
                f.write(f"            Reason: {vf.corrupt_reason}\n")
                f.write(f"            Size:   {fmt_size(vf.size_bytes)}\n\n")

    print(GREEN(f"✓ Text exported: {path}"))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def print_summary(good: List["VideoFile"], corrupt: List["VideoFile"],
                  total_scanned: int):
    print()
    print(BOLD("── Summary ─────────────────────────────────────────────"))
    total_size   = sum(vf.size_bytes for vf in good)
    corrupt_size = sum(vf.size_bytes for vf in corrupt)
    print(f"  Files scanned:    {total_scanned}")
    print(f"  Valid:            {GREEN(str(len(good)))}")
    print(f"  Corrupt:          {RED(str(len(corrupt))) if corrupt else GREEN('0')}")
    print(f"  Total size:       {fmt_size(total_size + corrupt_size)}")
    print(f"  Valid size:       {fmt_size(total_size)}")

    if good:
        codecs: Dict[str, int] = {}
        for vf in good:
            codecs[vf.codec] = codecs.get(vf.codec, 0) + 1
        top_codecs = sorted(codecs.items(), key=lambda x: -x[1])
        print(f"  Codecs:           " +
              ", ".join(f"{k}({v})" for k, v in top_codecs))

        scoreable = [vf for vf in good if vf.norm_efficiency < float("inf")]
        if scoreable:
            avg = sum(vf.norm_efficiency for vf in scoreable) / len(scoreable)
            best  = min(scoreable, key=lambda v: v.norm_efficiency)
            worst = max(scoreable, key=lambda v: v.norm_efficiency)
            print(f"  Avg norm-eff:     {fmt_efficiency(avg)}  "
                  f"(lower = more efficient, H.264-normalised)")
            print(f"  Best encoded:     {best.path.name}  "
                  f"[{best.codec} {fmt_efficiency(best.norm_efficiency)}]")
            print(f"  Worst encoded:    {worst.path.name}  "
                  f"[{worst.codec} {fmt_efficiency(worst.norm_efficiency)}]")

        grade_counts: Dict[str, int] = {}
        for vf in good:
            grade_counts[vf.grade] = grade_counts.get(vf.grade, 0) + 1
        order = ["excellent", "good", "fair", "poor", "terrible", "?"]
        grade_str = "  ".join(
            f"{g}:{grade_counts[g]}" for g in order if g in grade_counts
        )
        print(f"  Grade breakdown:  {grade_str}")

    print()


# ---------------------------------------------------------------------------
# Sort logic
# ---------------------------------------------------------------------------
SORT_KEYS = {
    "efficiency": lambda v: (v.norm_efficiency == float("inf"), v.norm_efficiency),
    "size":       lambda v: v.size_bytes,
    "bitrate":    lambda v: v.bitrate_bps,
    "resolution": lambda v: v.width * v.height,
    "codec":      lambda v: v.codec,
    "name":       lambda v: v.path.name.lower(),
    "fps":        lambda v: v.fps,
    "duration":   lambda v: v.duration_s,
}


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        prog="movie-scanner",
        description=(
            "Scan a directory for video files, detect corruption, and rank by "
            "codec-normalised encoding efficiency."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Efficiency metric: (bitrate / pixels) / codec_factor  →  H.264-equivalent bits/pixel
Grades are RELATIVE to your library (percentile-based):
  excellent  → best 20%   of your library
  good       → 20–45th percentile
  fair       → 45–70th percentile
  poor       → 70–88th percentile
  terrible   → worst 12%  of your library

Codec factors (vs H.264 = 1.0):  AV1=2.5  HEVC=1.8  VP9=1.4  MPEG4=0.7  MPEG2=0.4
"""
    )
    p.add_argument("directory", help="Directory to scan")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Recurse into subdirectories")
    p.add_argument("--deep", action="store_true",
                   help="Deep corruption scan: decode frames at 3 points (slower)")
    p.add_argument("--sort", choices=list(SORT_KEYS.keys()), default="efficiency",
                   help="Sort field (default: efficiency)")
    p.add_argument("--desc", action="store_true",
                   help="Sort descending")
    p.add_argument("--csv", metavar="FILE", help="Export results to CSV")
    p.add_argument("--txt", metavar="FILE", help="Export results to plain text")
    p.add_argument("--no-table", action="store_true",
                   help="Suppress terminal table")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                   help="Parallel probe workers (default: CPU count)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Suppress progress output")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = parse_args()

    print(BOLD("\n⚙  movie-scanner"))
    print(DIM("   Checking dependencies..."))
    check_dependencies()

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print(RED(f"✗ Not a directory: {directory}"))
        sys.exit(1)

    print(DIM(f"   Directory:  {directory}"))
    print(DIM(f"   Recursive:  {'yes' if args.recursive else 'no'}"))
    if args.deep:
        print(YELLOW("   Deep scan:  enabled (this will be slow)"))

    files = collect_files(directory, args.recursive)
    if not files:
        print(YELLOW(f"\n  No video files found in {directory}"))
        sys.exit(0)

    print(DIM(f"   Found {len(files)} video file(s). Probing with {args.jobs} worker(s)..."))
    print()

    results: List[VideoFile] = []
    done = 0

    def probe_one(path: Path) -> VideoFile:
        vf = probe_file(path)
        if args.deep:
            vf = deep_probe(vf)
        return vf

    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(probe_one, p): p for p in files}
        for future in as_completed(futures):
            done += 1
            vf = future.result()
            results.append(vf)
            if not args.quiet:
                status = RED("✗ corrupt") if vf.corrupt else GREEN("✓")
                pct = done * 100 // len(files)
                print(f"\r  [{pct:3d}%] {done}/{len(files)}  {status}  "
                      f"{_trunc(vf.path.name, 50):<50}", end="", flush=True)

    if not args.quiet:
        print()

    good    = [vf for vf in results if not vf.corrupt]
    corrupt = [vf for vf in results if vf.corrupt]

    # Assign percentile grades across the full valid set
    assign_grades(good)

    # Sort
    good.sort(key=SORT_KEYS[args.sort], reverse=args.desc)

    print_summary(good, corrupt, len(files))

    if not args.no_table:
        print(BOLD(f"── Valid Files ({len(good)}) ─────────────────────────────────────"))
        print_table(good, args.sort, args.desc)

    print_corrupt_section(corrupt)

    if args.csv:
        export_csv(good + corrupt, args.csv)
    if args.txt:
        export_txt(good, corrupt, args.txt, args.sort, args.desc)

    sys.exit(2 if corrupt else 0)


if __name__ == "__main__":
    main()
