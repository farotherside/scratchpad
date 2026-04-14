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
    --html <file>           Export results as a self-contained HTML report
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
from tempfile import NamedTemporaryFile
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
    vmaf_mean: Optional[float] = None
    vmaf_reference: str = ""

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

    # With only one file there's no meaningful distribution — skip grading
    if len(scoreable) < 2:
        for vf in scoreable:
            vf.percentile = 50.0
            vf.grade = "?"
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
def _ffmpeg_has_filter(filter_name: str) -> bool:
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            timeout=15,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    output = _decode_subprocess_output(result.stdout) + "\n" + _decode_subprocess_output(result.stderr)
    return filter_name in output


def check_dependencies(require_vmaf: bool = False):
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
    if require_vmaf and not _ffmpeg_has_filter("libvmaf"):
        print(RED("✗ ffmpeg was found, but it does not include the libvmaf filter."))
        print(DIM("  Install/build an ffmpeg package with libvmaf enabled, then rerun with --vmaf-reference."))
        sys.exit(1)
    print(GREEN("✓") + f" ffprobe: {shutil.which('ffprobe')}")


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------
def collect_files(directory: Path, recursive: bool) -> List[Path]:
    results = []
    it = directory.rglob("*") if recursive else directory.iterdir()
    for p in it:
        if not p.is_file():
            continue
        if p.name.startswith("._"):
            continue
        if p.suffix.lower() in VIDEO_EXTENSIONS:
            results.append(p)
    return sorted(results)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def _decode_subprocess_output(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def run_ffprobe(path: Path) -> Optional[dict]:
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        stdout_text = _decode_subprocess_output(result.stdout)
        if result.returncode != 0 or not stdout_text.strip():
            return None
        return json.loads(stdout_text)
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
            result = subprocess.run(cmd, capture_output=True, timeout=60)
            if result.returncode != 0:
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


def compute_vmaf(distorted_path: Path, reference_path: Path) -> Optional[float]:
    with NamedTemporaryFile(mode="w+b", suffix=".json", delete=True) as tmp:
        cmd = [
            "ffmpeg", "-hide_banner", "-nostats",
            "-i", str(distorted_path),
            "-i", str(reference_path),
            "-lavfi", "[0:v]setpts=PTS-STARTPTS[dist];[1:v]setpts=PTS-STARTPTS[ref];[dist][ref]libvmaf=log_fmt=json:log_path={}".format(tmp.name),
            "-f", "null", "-",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=3600)
            if result.returncode != 0:
                return None
            tmp.seek(0)
            raw = tmp.read()
            if not raw:
                return None
            data = json.loads(_decode_subprocess_output(raw))
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError):
            return None

    pooled = data.get("pooled_metrics", {})
    vmaf = pooled.get("vmaf", {}) if isinstance(pooled, dict) else {}
    mean = vmaf.get("mean") if isinstance(vmaf, dict) else None
    try:
        return float(mean) if mean is not None else None
    except (TypeError, ValueError):
        return None


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


def fmt_vmaf(vmaf: Optional[float]) -> str:
    return "N/A" if vmaf is None else f"{vmaf:.2f}"


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
    ("VMAF",      6, "right"),
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
        hdr.ljust(widths[i]) if COLUMNS[i][2] == "left" else hdr.rjust(widths[i])
        for i, hdr in enumerate(headers)
    ]
    separator = "─" * (sum(widths) + separators)

    print()
    print(BOLD(" ".join(header_cells)))
    print(DIM(separator))

    for vf in files:
        raw_cells = [
            fmt_efficiency(vf.norm_efficiency),
            fmt_efficiency(vf.raw_efficiency),
            fmt_vmaf(vf.vmaf_mean),
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
                fn = GRADE_COLOUR.get(vf.grade, lambda t: t)
                rendered = fn(truncated)
            elif i == 5:  # Grade
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
    "vmaf_mean", "vmaf_reference",
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
        "vmaf_mean":       fmt_vmaf(vf.vmaf_mean),
        "vmaf_reference":  vf.vmaf_reference,
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


# ---------------------------------------------------------------------------
# HTML export
# ---------------------------------------------------------------------------
GRADE_CSS = {
    "excellent": "#22c55e",   # green
    "good":      "#86efac",   # light green
    "fair":      "#facc15",   # yellow
    "poor":      "#f97316",   # orange
    "terrible":  "#ef4444",   # red
    "?":         "#6b7280",   # grey
}

def h(text: str) -> str:
    """HTML-escape a string."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


def export_html(good: List["VideoFile"], corrupt: List["VideoFile"],
               path: str, sort_by: str, descending: bool,
               directory: str, total_scanned: int):
    from datetime import datetime

    total_size   = sum(vf.size_bytes for vf in good)
    corrupt_size = sum(vf.size_bytes for vf in corrupt)

    grade_counts: Dict[str, int] = {}
    for vf in good:
        grade_counts[vf.grade] = grade_counts.get(vf.grade, 0) + 1

    scoreable = [vf for vf in good if vf.norm_efficiency < float("inf")]
    avg_eff = (sum(vf.norm_efficiency for vf in scoreable) / len(scoreable)
               if scoreable else None)
    best  = min(scoreable, key=lambda v: v.norm_efficiency) if scoreable else None
    worst = max(scoreable, key=lambda v: v.norm_efficiency) if scoreable else None

    codecs: Dict[str, int] = {}
    for vf in good:
        codecs[vf.codec] = codecs.get(vf.codec, 0) + 1
    top_codecs = sorted(codecs.items(), key=lambda x: -x[1])

    grade_order = ["excellent", "good", "fair", "poor", "terrible", "?"]

    # Plain-English grade descriptions for tooltips
    GRADE_DESC = {
        "excellent": "Best-encoded 20% of your library — very efficient for its resolution and codec.",
        "good":      "Well-encoded — 20th to 45th percentile. Efficient use of space.",
        "fair":      "Average encoding — 45th to 70th percentile. Some room to re-encode.",
        "poor":      "Bloated encoding — 70th to 88th percentile. File is larger than it needs to be.",
        "terrible":  "Worst-encoded 12% of your library — very bloated for its resolution and codec.",
        "?":         "Could not be graded (unknown bitrate or only one file scanned).",
    }

    def grade_badge(grade: str) -> str:
        colour = GRADE_CSS.get(grade, "#6b7280")
        text_colour = "#111" if grade in ("excellent", "good", "fair") else "#fff"
        tip = h(GRADE_DESC.get(grade, ""))
        return (f'<span class="badge has-tip" '
                f'style="background:{colour};color:{text_colour}" '
                f'data-tip="{tip}">'
                f'{h(grade)}</span>')

    def norm_eff_cell(vf: "VideoFile") -> str:
        colour = GRADE_CSS.get(vf.grade, "#6b7280")
        return f'<span style="color:{colour};font-weight:600">{h(fmt_efficiency(vf.norm_efficiency))}</span>'

    # Build rows — filename cell gets a rich hover card
    rows_html = []
    for vf in good:
        d = file_to_dict(vf)
        audio_str = h(", ".join(vf.audio_codecs) if vf.audio_codecs else "none")
        vmaf_str  = h(fmt_vmaf(vf.vmaf_mean))
        tip_lines = [
            f"Path: {h(str(vf.path))}",
            f"Size: {h(fmt_size(vf.size_bytes))}",
            f"Resolution: {h(vf.resolution)}",
            f"Bitrate: {h(d['bitrate_fmt'])}",
            f"Duration: {h(vf.duration_hms)}",
            f"FPS: {h(d['fps'])}",
            f"Codec: {h(vf.codec)}",
            f"Audio: {audio_str}",
            f"Format: {h(vf.format_name)}",
        ]
        if vf.vmaf_mean is not None:
            tip_lines.append(f"VMAF: {vmaf_str}")
        file_tip = h("\n".join(tip_lines))
        rows_html.append(f"""
        <tr class="grade-{h(vf.grade)}">
          <td class="mono num">{norm_eff_cell(vf)}</td>
          <td class="mono num dim">{h(d['raw_efficiency'])}</td>
          <td><code>{h(d['codec'])}</code></td>
          <td class="dim num">{h(d['codec_factor'])}x</td>
          <td>{grade_badge(vf.grade)}</td>
          <td class="dim num">{h(d['percentile'])}%</td>
          <td class="mono num">{h(fmt_size(vf.size_bytes))}</td>
          <td>{h(vf.resolution)}</td>
          <td class="mono num dim">{h(d['bitrate_fmt'])}</td>
          <td class="dim">{h(vf.duration_hms)}</td>
          <td class="filename has-tip" data-tip="{file_tip}">{h(vf.path.name)}</td>
        </tr>""")

    corrupt_rows = []
    for vf in corrupt:
        corrupt_tip = h(f"Path: {str(vf.path)}\nSize: {fmt_size(vf.size_bytes)}\nReason: {vf.corrupt_reason}")
        corrupt_rows.append(f"""
        <tr class="corrupt-row">
          <td colspan="3" class="filename has-tip" data-tip="{corrupt_tip}">
            <span class="corrupt-x">✗</span> {h(vf.path.name)}
          </td>
          <td colspan="4" class="dim">{h(vf.corrupt_reason)}</td>
          <td colspan="4" class="dim">{h(fmt_size(vf.size_bytes))}</td>
        </tr>""")

    grade_pills = "".join(
        f'<span class="pill has-tip" style="background:{GRADE_CSS.get(g,"#6b7280")}" data-tip="{h(GRADE_DESC.get(g,""))}">{g}: {grade_counts.get(g,0)}</span>'
        for g in grade_order if g in grade_counts
    )

    codec_pills = "".join(
        f'<span class="pill pill-neutral">{h(k)}: {v}</span>'
        for k, v in top_codecs
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>movie-scanner report</title>
<style>
  :root {{
    --bg:      #0f1117;
    --surface: #1a1d27;
    --border:  #2a2d3a;
    --text:    #e2e8f0;
    --dim:     #64748b;
    --accent:  #6366f1;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px;
    padding: 2rem;
  }}
  h1 {{ font-size: 1.4rem; font-weight: 700; margin-bottom: .25rem; color: #f1f5f9; }}
  h2 {{ font-size: 1rem; font-weight: 600; margin: 1.5rem 0 .6rem; color: #cbd5e1; }}
  .meta {{ color: var(--dim); font-size: .8rem; margin-bottom: 1.5rem; }}
  .summary {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: .75rem;
    margin-bottom: 1.5rem;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: .75rem 1rem;
  }}
  .card .label {{ font-size: .7rem; text-transform: uppercase;
                  letter-spacing: .05em; color: var(--dim); }}
  .card .value {{ font-size: 1.1rem; font-weight: 700; margin-top: .2rem; }}
  .pills {{ display: flex; flex-wrap: wrap; gap: .4rem; margin-bottom: 1.2rem; }}
  .pill {{
    padding: .2rem .55rem;
    border-radius: 999px;
    font-size: .72rem;
    font-weight: 600;
    color: #111;
    cursor: default;
  }}
  .pill-neutral {{ background: var(--border); color: var(--text); }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  thead th {{
    background: var(--surface);
    color: var(--dim);
    font-weight: 600;
    text-transform: uppercase;
    font-size: .68rem;
    letter-spacing: .04em;
    padding: .55rem .6rem;
    border-bottom: 1px solid var(--border);
    text-align: left;
    position: sticky;
    top: 0;
    cursor: pointer;
    user-select: none;
  }}
  thead th:hover {{ color: var(--text); }}
  thead th.sorted {{ color: var(--accent); }}
  thead th.num, td.num {{ text-align: right; }}
  tbody tr {{
    border-bottom: 1px solid var(--border);
    transition: background .1s;
  }}
  tbody tr:hover {{ background: var(--surface); }}
  td {{
    padding: .45rem .6rem;
    vertical-align: middle;
    white-space: nowrap;
  }}
  td.filename {{
    max-width: 320px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: #cbd5e1;
    cursor: default;
  }}
  .mono {{ font-family: 'SF Mono', 'Fira Code', monospace; }}
  .dim {{ color: var(--dim); }}
  code {{
    background: var(--border);
    padding: .1rem .35rem;
    border-radius: 4px;
    font-size: .8rem;
  }}
  .badge {{
    display: inline-block;
    padding: .15rem .5rem;
    border-radius: 999px;
    font-size: .7rem;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .04em;
    cursor: default;
  }}
  .corrupt-row td {{ color: #ef4444; }}
  .corrupt-row .dim {{ color: #f87171; }}
  .corrupt-x {{ font-weight: 900; margin-right: .3rem; }}
  input#search {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: .4rem .8rem;
    font-size: .85rem;
    outline: none;
    width: 260px;
  }}
  input#search:focus {{ border-color: var(--accent); }}
  .toolbar {{ display: flex; align-items: center; gap: 1rem; margin-bottom: .75rem; flex-wrap: wrap; }}
  label.filter-label {{
    color: var(--dim);
    font-size: .8rem;
    display: flex;
    align-items: center;
    gap: .35rem;
    cursor: pointer;
  }}
  footer {{ margin-top: 2rem; color: var(--dim); font-size: .75rem; }}
  /* Tooltip */
  .tooltip-box {{
    position: fixed;
    z-index: 9999;
    background: #1e2130;
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: .6rem .85rem;
    font-size: .78rem;
    line-height: 1.6;
    color: var(--text);
    max-width: 420px;
    white-space: pre-wrap;
    word-break: break-all;
    box-shadow: 0 8px 32px rgba(0,0,0,.6);
    pointer-events: none;
    opacity: 0;
    transition: opacity .12s;
  }}
  .tooltip-box.visible {{ opacity: 1; }}
  .has-tip {{ cursor: help; }}
  thead th.has-tip {{ cursor: pointer; }}
  .th-label {{ border-bottom: 1px dotted var(--dim); }}
</style>
</head>
<body>
<div class="tooltip-box" id="tt"></div>
<h1>⚙ movie-scanner report</h1>
<div class="meta">
  Directory: <code>{h(directory)}</code> &nbsp;·&nbsp;
  Sorted by: <strong>{h(sort_by)}</strong> {'↓' if descending else '↑'} &nbsp;·&nbsp;
  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
</div>

<div class="summary">
  <div class="card"><div class="label">Files scanned</div><div class="value">{total_scanned}</div></div>
  <div class="card"><div class="label">Valid</div><div class="value" style="color:#22c55e">{len(good)}</div></div>
  <div class="card"><div class="label">Corrupt / unreadable</div><div class="value" style="color:{'#ef4444' if corrupt else '#22c55e'}">{len(corrupt)}</div></div>
  <div class="card"><div class="label">Total size</div><div class="value">{h(fmt_size(total_size + corrupt_size))}</div></div>
  {'<div class="card has-tip" data-tip="Average codec-normalised bits per pixel across all valid files. Lower = more efficient library overall."><div class="label">Avg efficiency</div><div class="value mono">' + h(fmt_efficiency(avg_eff)) + '</div></div>' if avg_eff else ''}
  {'<div class="card has-tip" data-tip="' + h("Most efficiently encoded file: uses the fewest bits per pixel relative to its codec.") + '"><div class="label">Best encoded</div><div class="value" style="font-size:.8rem;color:#22c55e">' + h(best.path.name[:28]) + '</div></div>' if best else ''}
  {'<div class="card has-tip" data-tip="' + h("Most bloated file: uses the most bits per pixel relative to its codec. Good re-encode candidate.") + '"><div class="label">Worst encoded</div><div class="value" style="font-size:.8rem;color:#ef4444">' + h(worst.path.name[:28]) + '</div></div>' if worst else ''}
</div>

<h2>Grade breakdown <span class="dim" style="font-size:.75rem;font-weight:400">(hover a grade for explanation)</span></h2>
<div class="pills">{grade_pills}</div>

<h2>Codecs in library</h2>
<div class="pills">{codec_pills}</div>

<h2>Files ({len(good)})</h2>
<div class="toolbar">
  <input id="search" type="text" placeholder="Filter by filename…" oninput="filterTable(this.value)">
  <label class="filter-label">
    <input type="checkbox" id="poor-only" onchange="filterTable(document.getElementById('search').value)">
    Bloated only (poor / terrible)
  </label>
</div>
<table id="main-table">
  <thead>
    <tr>
      <th class="num sorted has-tip" onclick="sortTable(0)" data-tip="Normalised Efficiency — codec-adjusted bits per pixel. Lower = better encoded.&#10;Accounts for how much more efficient modern codecs (HEVC, AV1) are vs H.264.&#10;H.264 is the baseline (factor 1.0). An HEVC file at the same NormEff as an H.264 file&#10;delivers significantly better picture quality per bit."><span class="th-label">NormEff ↕</span></th>
      <th class="num has-tip" onclick="sortTable(1)" data-tip="Raw Efficiency — raw bits per pixel with NO codec adjustment.&#10;Useful for comparing files encoded with the same codec.&#10;Use NormEff for cross-codec comparisons."><span class="th-label">RawEff ↕</span></th>
      <th class="has-tip" onclick="sortTable(2)" data-tip="Video codec used to encode this file."><span class="th-label">Codec ↕</span></th>
      <th class="num has-tip" onclick="sortTable(3)" data-tip="Codec efficiency factor vs H.264 = 1.0.&#10;NormEff = RawEff ÷ Factor.&#10;Examples: AV1=2.5x  HEVC=1.8x  VP9=1.4x  H.264=1.0x  MPEG4=0.7x"><span class="th-label">Factor ↕</span></th>
      <th class="has-tip" onclick="sortTable(4)" data-tip="Encoding quality grade — percentile rank within this library.&#10;excellent = best 20%  ·  good = 20–45%  ·  fair = 45–70%&#10;poor = 70–88%  ·  terrible = worst 12%"><span class="th-label">Grade ↕</span></th>
      <th class="num has-tip" onclick="sortTable(5)" data-tip="Percentile rank (0 = most efficient, 100 = most bloated).&#10;Based on NormEff position within this library."><span class="th-label">Pctile ↕</span></th>
      <th class="num" onclick="sortTable(6)">Size ↕</th>
      <th onclick="sortTable(7)">Resolution ↕</th>
      <th class="num" onclick="sortTable(8)">Bitrate ↕</th>
      <th onclick="sortTable(9)">Duration ↕</th>
      <th class="has-tip" onclick="sortTable(10)" data-tip="Hover the filename to see full path, audio tracks, FPS, and format."><span class="th-label">Filename ↕</span></th>
    </tr>
  </thead>
  <tbody id="main-tbody">
    {''.join(rows_html)}
  </tbody>
</table>

{'<h2>⚠ Corrupt / Unreadable Files (' + str(len(corrupt)) + ')</h2><p style="color:var(--dim);font-size:.8rem;margin-bottom:.75rem">These files could not be read by ffprobe. Hover the filename for the full path and reason.</p><table><thead><tr><th colspan="3">Filename</th><th colspan="4">Reason</th><th colspan="4">Size</th></tr></thead><tbody>' + ''.join(corrupt_rows) + '</tbody></table>' if corrupt else ''}

<footer>
  movie-scanner &nbsp;·&nbsp;
  <strong>NormEff</strong> = codec-normalised bits/pixel (H.264 baseline, lower = better) &nbsp;·&nbsp;
  <strong>Grades</strong> are percentile-based relative to this library, not fixed thresholds.
</footer>

<script>
  // ── Tooltip ──────────────────────────────────────────────────────────────
  const tt = document.getElementById('tt');
  let ttVisible = false;

  document.addEventListener('mouseover', e => {{
    const el = e.target.closest('.has-tip');
    if (!el) return;
    const text = el.dataset.tip;
    if (!text) return;
    tt.textContent = text;
    tt.classList.add('visible');
    ttVisible = true;
  }});

  document.addEventListener('mouseout', e => {{
    if (!e.target.closest('.has-tip')) return;
    tt.classList.remove('visible');
    ttVisible = false;
  }});

  document.addEventListener('mousemove', e => {{
    if (!ttVisible) return;
    const pad = 16;
    let x = e.clientX + pad, y = e.clientY + pad;
    const bw = tt.offsetWidth, bh = tt.offsetHeight;
    if (x + bw > window.innerWidth  - pad) x = e.clientX - bw - pad;
    if (y + bh > window.innerHeight - pad) y = e.clientY - bh - pad;
    tt.style.left = x + 'px';
    tt.style.top  = y + 'px';
  }});

  // ── Client-side sort ──────────────────────────────────────────────────────
  let sortCol = 0, sortAsc = true;
  const numCols = new Set([0,1,3,5,8]);   // pure numeric cols (not Size col 6)

  function sizeToBytes(s) {{
    const m = s.match(/(\\d+\\.?\\d*)\\s*(GB|MB|KB|B)/i);
    if (!m) return 0;
    const n = parseFloat(m[1]);
    const u = m[2].toUpperCase();
    return n * (u === 'GB' ? 1e9 : u === 'MB' ? 1e6 : u === 'KB' ? 1e3 : 1);
  }}

  function cellVal(row, col) {{
    return row.cells[col]?.innerText.trim() ?? '';
  }}

  function filterTable(query) {{
    const q     = query.toLowerCase();
    const poor  = document.getElementById('poor-only').checked;
    Array.from(document.getElementById('main-tbody').rows).forEach(row => {{
      const name = cellVal(row, 10).toLowerCase();
      const grade = row.className.replace('grade-', '');
      const isBloated = grade === 'poor' || grade === 'terrible';
      row.style.display = (name.includes(q) && (!poor || isBloated)) ? '' : 'none';
    }});
  }}

  function sortTable(col) {{
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = true; }}

    const tbody = document.getElementById('main-tbody');
    const rows  = Array.from(tbody.rows);
    rows.sort((a, b) => {{
      let av = cellVal(a, col), bv = cellVal(b, col);
      if (av === 'N/A') av = sortAsc ? '99999' : '-1';
      if (bv === 'N/A') bv = sortAsc ? '99999' : '-1';
      if (col === 6) {{
        return sortAsc ? sizeToBytes(av) - sizeToBytes(bv) : sizeToBytes(bv) - sizeToBytes(av);
      }}
      if (numCols.has(col)) {{
        av = parseFloat(av.replace(/[^0-9.]/g, '')) || 0;
        bv = parseFloat(bv.replace(/[^0-9.]/g, '')) || 0;
        return sortAsc ? av - bv : bv - av;
      }}
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    }});
    rows.forEach(r => tbody.appendChild(r));

    document.querySelectorAll('thead th').forEach((th, i) => {{
      th.classList.toggle('sorted', i === col);
    }});
  }}
</script>
</body>
</html>
"""

    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(GREEN(f"✓ HTML report: {path}"))


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

        col_w = [8, 8, 6, 7, 7, 9, 7, 9, 12, 10, 9]
        headers = ["NormEff", "RawEff", "VMAF", "Codec", "Factor", "Grade",
                   "Pctile", "Size", "Resolution", "Bitrate", "Duration", "Filename"]
        f.write("  ".join(
            hdr.rjust(col_w[i]) if i < len(col_w) else hdr
            for i, hdr in enumerate(headers)
        ) + "\n")
        f.write("-" * 100 + "\n")

        for vf in good:
            d = file_to_dict(vf)
            cells = [
                d["norm_efficiency"].rjust(8),
                d["raw_efficiency"].rjust(8),
                d["vmaf_mean"].rjust(6),
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

        vmaf_scores = [vf.vmaf_mean for vf in good if vf.vmaf_mean is not None]
        if vmaf_scores:
            avg_vmaf = sum(vmaf_scores) / len(vmaf_scores)
            best_vmaf = max((vf for vf in good if vf.vmaf_mean is not None), key=lambda v: v.vmaf_mean)
            worst_vmaf = min((vf for vf in good if vf.vmaf_mean is not None), key=lambda v: v.vmaf_mean)
            print(f"  Avg VMAF:         {fmt_vmaf(avg_vmaf)}")
            print(f"  Best VMAF:        {best_vmaf.path.name}  [{fmt_vmaf(best_vmaf.vmaf_mean)}]")
            print(f"  Worst VMAF:       {worst_vmaf.path.name}  [{fmt_vmaf(worst_vmaf.vmaf_mean)}]")

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
    "vmaf":       lambda v: (v.vmaf_mean is None, -(v.vmaf_mean or 0.0)),
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
            "Scan a directory for video files, detect corruption, rank by "
            "codec-normalised encoding efficiency, and optionally compute VMAF "
            "against a reference encode."
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
    p.add_argument("--vmaf-reference", metavar="FILE",
                   help="Compute VMAF for each scanned file against this reference file")
    p.add_argument("--desc", action="store_true",
                   help="Sort descending")
    p.add_argument("--csv", metavar="FILE", help="Export results to CSV")
    p.add_argument("--txt", metavar="FILE", help="Export results to plain text")
    p.add_argument("--html", metavar="FILE", nargs="?", const="",
                   help="Export results as a self-contained HTML report. "
                        "Omit FILE to auto-name report.html in the scanned directory.")
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
    check_dependencies(require_vmaf=bool(args.vmaf_reference))

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print(RED(f"✗ Not a directory: {directory}"))
        sys.exit(1)

    print(DIM(f"   Directory:  {directory}"))
    print(DIM(f"   Recursive:  {'yes' if args.recursive else 'no'}"))
    reference_path = None
    if args.vmaf_reference:
        reference_path = Path(args.vmaf_reference).expanduser().resolve()
        if not reference_path.is_file():
            print(RED(f"✗ Not a file: {reference_path}"))
            sys.exit(1)
        print(YELLOW(f"   VMAF ref:   {reference_path}"))
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

    if reference_path and good:
        if not args.quiet:
            print(DIM(f"   Computing VMAF against reference for {len(good)} valid file(s)..."))
        vmaf_done = 0
        for vf in good:
            vf.vmaf_reference = str(reference_path)
            if vf.path == reference_path:
                vf.vmaf_mean = 100.0
            else:
                vf.vmaf_mean = compute_vmaf(vf.path, reference_path)
            vmaf_done += 1
            if not args.quiet:
                print(f"\r  [VMAF {vmaf_done:3d}/{len(good)}] {fmt_vmaf(vf.vmaf_mean):>6}  {_trunc(vf.path.name, 50):<50}", end="", flush=True)
        if not args.quiet:
            print()

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

    # HTML: auto-generate if --html was given with no filename, or
    # --html FILE was given explicitly.  Default (no --html flag) = no HTML.
    if args.html is not None:
        html_path = args.html if args.html else str(directory / "report.html")
        export_html(good, corrupt, html_path, args.sort, args.desc,
                    str(directory), len(files))

    sys.exit(2 if corrupt else 0)


if __name__ == "__main__":
    main()
