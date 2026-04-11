#!/usr/bin/env python3
"""
mediascan.py — Media library audit tool
Scans a directory for video files, identifies corruption, and ranks files
by encoding efficiency (bitrate per pixel per second).

Usage:
    mediascan.py [OPTIONS] <directory>

Options:
    -r, --recursive         Recurse into subdirectories
    --deep                  Deep corruption scan (decodes frames, slower)
    --sort <field>          Sort by: efficiency, size, bitrate, resolution, codec, name
                            (default: efficiency)
    --desc                  Sort descending (default: ascending)
    --csv <file>            Export results to CSV
    --txt <file>            Export results to plain text
    --no-table              Suppress terminal table output
    --show-corrupt          Show corrupt files inline (default: separate section)
    -j, --jobs <n>          Parallel probe jobs (default: CPU count)
    -q, --quiet             Suppress progress output
    -h, --help              Show this help
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Known video extensions that ffmpeg/ffprobe can handle.
# Sourced from: ffmpeg -formats | grep D (demuxers) — common containers only.
# We cast a wide net; ffprobe will tell us if a file is actually a video.
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
    bitrate_bps: float = 0.0   # overall container bitrate
    fps: float = 0.0
    format_name: str = ""
    audio_codecs: list = field(default_factory=list)

    # computed
    efficiency: float = 0.0    # bits per pixel (lower = more efficient)

    @property
    def size_mb(self) -> float:
        return self.size_bytes / (1024 ** 2)

    @property
    def size_gb(self) -> float:
        return self.size_bytes / (1024 ** 3)

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
        """
        Efficiency = bitrate (bps) / pixels
        Units: bits per pixel per second.
        Lower is better (more efficient encoding).
        A well-encoded 1080p H.264 file is roughly 0.05–0.15 bps/px.
        """
        if self.pixels > 0 and self.bitrate_bps > 0:
            self.efficiency = self.bitrate_bps / self.pixels
        else:
            self.efficiency = float("inf")


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
    print(GREEN("✓") + f" ffprobe found: {shutil.which('ffprobe')}")


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------
def collect_files(directory: Path, recursive: bool) -> list[Path]:
    results = []
    if recursive:
        it = directory.rglob("*")
    else:
        it = directory.iterdir()
    for p in it:
        if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            results.append(p)
    return sorted(results)


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------
def run_ffprobe(path: Path) -> Optional[dict]:
    """Run ffprobe and return parsed JSON, or None on failure."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams", "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def probe_file(path: Path) -> VideoFile:
    vf = VideoFile(path=path, size_bytes=path.stat().st_size)
    data = run_ffprobe(path)

    if data is None:
        vf.corrupt = True
        vf.corrupt_reason = "ffprobe failed to read file"
        return vf

    vf.probe_ok = True

    # -- Format / container --
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

    # -- Streams --
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
    vf.width = int(vs.get("width", 0))
    vf.height = int(vs.get("height", 0))

    # FPS
    r_fps = vs.get("r_frame_rate", "0/1")
    try:
        num, den = r_fps.split("/")
        vf.fps = float(num) / float(den) if float(den) != 0 else 0.0
    except (ValueError, ZeroDivisionError):
        vf.fps = 0.0

    # If container bitrate is 0, try stream-level
    if vf.bitrate_bps == 0:
        try:
            vf.bitrate_bps = float(vs.get("bit_rate", 0))
        except (ValueError, TypeError):
            pass

    # Flag suspicious files (duration=0, zero-pixel frame)
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


def deep_probe(vf: VideoFile) -> VideoFile:
    """
    Attempt to decode a sample of frames to detect corruption that ffprobe
    misses. Checks 3 points: 10%, 50%, 90% through the file.
    Marks as corrupt if any decode attempt fails.
    """
    if vf.corrupt or not vf.probe_ok or vf.duration_s <= 0:
        return vf

    seek_points = [0.10, 0.50, 0.90]
    for frac in seek_points:
        seek_s = vf.duration_s * frac
        cmd = [
            "ffmpeg",
            "-ss", str(seek_s),
            "-i", str(vf.path),
            "-frames:v", "1",
            "-f", "null",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            # ffmpeg writes to stderr; a successful decode contains "frame="
            if "frame=" not in result.stderr and result.returncode != 0:
                vf.corrupt = True
                vf.corrupt_reason = f"frame decode failed at {frac*100:.0f}% ({seek_s:.1f}s)"
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
    else:
        return f"{size_bytes / 1024:.0f} KB"


def fmt_efficiency(eff: float) -> str:
    if eff == float("inf"):
        return "N/A"
    # Express as bits-per-pixel (multiply by 1 — already in bps/px)
    # Typical range: 0.01 (excellent) to 5.0+ (terrible)
    return f"{eff:.4f}"


def fmt_bitrate(bps: float) -> str:
    if bps <= 0:
        return "N/A"
    if bps >= 1_000_000:
        return f"{bps/1_000_000:.1f} Mbps"
    elif bps >= 1_000:
        return f"{bps/1_000:.0f} Kbps"
    return f"{bps:.0f} bps"


def efficiency_label(eff: float) -> str:
    """Qualitative label for terminal colour coding."""
    if eff == float("inf"):
        return "?"
    if eff < 0.05:
        return "excellent"
    if eff < 0.15:
        return "good"
    if eff < 0.40:
        return "fair"
    if eff < 1.0:
        return "poor"
    return "terrible"


def efficiency_colour(eff: float) -> str:
    label = efficiency_label(eff)
    mapping = {
        "excellent": GREEN,
        "good":      GREEN,
        "fair":      YELLOW,
        "poor":      RED,
        "terrible":  RED,
        "?":         DIM,
    }
    fn = mapping.get(label, lambda t: t)
    return fn(fmt_efficiency(eff))


# ---------------------------------------------------------------------------
# Terminal table renderer (no third-party deps)
# ---------------------------------------------------------------------------
COLUMNS = [
    ("Efficiency",  10, "right"),
    ("Grade",        9, "left"),
    ("Size",         9, "right"),
    ("Resolution",  12, "left"),
    ("Codec",        8, "left"),
    ("Bitrate",     10, "right"),
    ("Duration",     9, "right"),
    ("FPS",          6, "right"),
    ("Filename",     0, "left"),   # 0 = fill remaining
]

def _trunc(s: str, width: int) -> str:
    if width == 0:
        return s
    return s[:width] if len(s) <= width else s[:width-1] + "…"


def print_table(files: list[VideoFile], sort_by: str, descending: bool):
    if not files:
        print(DIM("  (no valid files)"))
        return

    # Compute terminal width
    try:
        term_width = os.get_terminal_size().columns
    except OSError:
        term_width = 120

    # Fixed columns total width
    fixed_total = sum(w for _, w, _ in COLUMNS if w > 0)
    separators  = len(COLUMNS) - 1
    filename_width = max(20, term_width - fixed_total - separators - 2)

    def row_cells(vf: VideoFile) -> list[str]:
        return [
            fmt_efficiency(vf.efficiency),
            efficiency_label(vf.efficiency),
            fmt_size(vf.size_bytes),
            vf.resolution,
            vf.codec or "unknown",
            fmt_bitrate(vf.bitrate_bps),
            vf.duration_hms,
            f"{vf.fps:.2f}" if vf.fps > 0 else "N/A",
            vf.path.name,
        ]

    def colour_row(vf: VideoFile, cells: list[str]) -> list[str]:
        coloured = list(cells)
        coloured[0] = efficiency_colour(vf.efficiency)
        grade = efficiency_label(vf.efficiency)
        grade_fns = {"excellent": GREEN, "good": GREEN, "fair": YELLOW,
                     "poor": RED, "terrible": RED}
        coloured[1] = grade_fns.get(grade, DIM)(cells[1])
        return coloured

    # Header
    widths = [w if w > 0 else filename_width for _, w, _ in COLUMNS]
    headers = [col[0] for col in COLUMNS]
    header_cells = [h.ljust(widths[i]) if COLUMNS[i][2] == "left"
                    else h.rjust(widths[i]) for i, h in enumerate(headers)]
    separator = "─" * (sum(widths) + separators)

    print()
    print(BOLD(" ".join(header_cells)))
    print(DIM(separator))

    for vf in files:
        cells = row_cells(vf)
        coloured = colour_row(vf, cells)
        out_cells = []
        for i, (cell, ccell) in enumerate(zip(cells, coloured)):
            w = widths[i]
            _, _, align = COLUMNS[i]
            raw_truncated = _trunc(cell, w)
            # Re-apply colour after truncation
            if cell != ccell and cell == raw_truncated:
                rendered = ccell
            else:
                rendered = _trunc(cell, w)
                # recolour after trunc
                if ccell != cell:
                    fn_map = {
                        0: lambda t: efficiency_colour(vf.efficiency),
                    }
                    if i == 0:
                        rendered = efficiency_colour(vf.efficiency)
                    elif i == 1:
                        grade = efficiency_label(vf.efficiency)
                        grade_fns = {"excellent": GREEN, "good": GREEN,
                                     "fair": YELLOW, "poor": RED, "terrible": RED}
                        rendered = grade_fns.get(grade, DIM)(raw_truncated)

            # Pad (ANSI codes don't count toward visible width)
            visible_len = len(raw_truncated)
            padding = w - visible_len
            if align == "right":
                out_cells.append(" " * padding + rendered)
            else:
                out_cells.append(rendered + " " * padding)

        print(" ".join(out_cells))

    print(DIM(separator))
    print(f"  {len(files)} files | sort: {sort_by} {'↓' if descending else '↑'}")


def print_corrupt_section(corrupt: list[VideoFile]):
    if not corrupt:
        return
    print()
    print(RED(BOLD(f"⚠  Corrupt / Unreadable Files ({len(corrupt)})")))
    print(DIM("─" * 60))
    for vf in corrupt:
        print(f"  {RED('✗')} {vf.path.name}")
        print(DIM(f"       {vf.path}"))
        print(DIM(f"       Reason: {vf.corrupt_reason}"))
        print(DIM(f"       Size:   {fmt_size(vf.size_bytes)}"))
    print()


# ---------------------------------------------------------------------------
# CSV / TXT export
# ---------------------------------------------------------------------------
CSV_FIELDS = [
    "path", "filename", "size_bytes", "size_mb",
    "codec", "width", "height", "resolution",
    "duration_s", "duration_hms", "fps",
    "bitrate_bps", "bitrate_fmt",
    "efficiency", "efficiency_grade",
    "format_name", "audio_codecs",
    "corrupt", "corrupt_reason",
]

def file_to_dict(vf: VideoFile) -> dict:
    return {
        "path":             str(vf.path),
        "filename":         vf.path.name,
        "size_bytes":       vf.size_bytes,
        "size_mb":          f"{vf.size_mb:.2f}",
        "codec":            vf.codec,
        "width":            vf.width,
        "height":           vf.height,
        "resolution":       vf.resolution,
        "duration_s":       f"{vf.duration_s:.2f}",
        "duration_hms":     vf.duration_hms,
        "fps":              f"{vf.fps:.3f}",
        "bitrate_bps":      f"{vf.bitrate_bps:.0f}",
        "bitrate_fmt":      fmt_bitrate(vf.bitrate_bps),
        "efficiency":       fmt_efficiency(vf.efficiency),
        "efficiency_grade": efficiency_label(vf.efficiency),
        "format_name":      vf.format_name,
        "audio_codecs":     ", ".join(vf.audio_codecs),
        "corrupt":          "yes" if vf.corrupt else "no",
        "corrupt_reason":   vf.corrupt_reason,
    }


def export_csv(all_files: list[VideoFile], path: str):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for vf in all_files:
            writer.writerow(file_to_dict(vf))
    print(GREEN(f"✓ CSV exported: {path}"))


def export_txt(good: list[VideoFile], corrupt: list[VideoFile], path: str,
               sort_by: str, descending: bool):
    with open(path, "w", encoding="utf-8") as f:
        f.write("MEDIASCAN REPORT\n")
        f.write("=" * 80 + "\n\n")

        f.write(f"Valid files ({len(good)}) — sorted by {sort_by} "
                f"({'desc' if descending else 'asc'})\n")
        f.write("-" * 80 + "\n")
        col_w = [10, 9, 9, 13, 8, 10, 9, 6]
        headers = ["Efficiency", "Grade", "Size", "Resolution",
                   "Codec", "Bitrate", "Duration", "FPS", "Filename"]
        header_line = "  ".join(
            h.rjust(col_w[i]) if i < len(col_w) else h
            for i, h in enumerate(headers)
        )
        f.write(header_line + "\n")
        f.write("-" * 80 + "\n")

        for vf in good:
            d = file_to_dict(vf)
            cells = [
                d["efficiency"].rjust(10),
                d["efficiency_grade"].ljust(9),
                d["size_mb"].rjust(7) + " MB",
                d["resolution"].ljust(13),
                d["codec"].ljust(8),
                d["bitrate_fmt"].rjust(10),
                d["duration_hms"].rjust(9),
                d["fps"].rjust(6),
                d["filename"],
            ]
            f.write("  ".join(cells) + "\n")

        if corrupt:
            f.write("\n" + "=" * 80 + "\n")
            f.write(f"CORRUPT / UNREADABLE FILES ({len(corrupt)})\n")
            f.write("-" * 80 + "\n")
            for vf in corrupt:
                f.write(f"  [CORRUPT] {vf.path}\n")
                f.write(f"            Reason: {vf.corrupt_reason}\n")
                f.write(f"            Size:   {fmt_size(vf.size_bytes)}\n\n")

    print(GREEN(f"✓ Text exported: {path}"))


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------
def print_summary(good: list[VideoFile], corrupt: list[VideoFile],
                  total_scanned: int):
    print()
    print(BOLD("── Summary ─────────────────────────────────────"))
    total_size = sum(vf.size_bytes for vf in good)
    corrupt_size = sum(vf.size_bytes for vf in corrupt)
    print(f"  Files scanned:   {total_scanned}")
    print(f"  Valid:           {GREEN(str(len(good)))}")
    print(f"  Corrupt:         {RED(str(len(corrupt))) if corrupt else GREEN('0')}")
    print(f"  Total size:      {fmt_size(total_size + corrupt_size)}")
    print(f"  Valid size:      {fmt_size(total_size)}")

    if good:
        codecs: dict[str, int] = {}
        for vf in good:
            codecs[vf.codec] = codecs.get(vf.codec, 0) + 1
        top_codecs = sorted(codecs.items(), key=lambda x: -x[1])
        print(f"  Codecs:          " +
              ", ".join(f"{k}({v})" for k, v in top_codecs))

        valid_eff = [vf for vf in good if vf.efficiency < float("inf")]
        if valid_eff:
            avg_eff = sum(vf.efficiency for vf in valid_eff) / len(valid_eff)
            best = min(valid_eff, key=lambda v: v.efficiency)
            worst = max(valid_eff, key=lambda v: v.efficiency)
            print(f"  Avg efficiency:  {fmt_efficiency(avg_eff)}")
            print(f"  Best encoded:    {best.path.name} ({fmt_efficiency(best.efficiency)})")
            print(f"  Worst encoded:   {worst.path.name} ({fmt_efficiency(worst.efficiency)})")

    print()


# ---------------------------------------------------------------------------
# Sort logic
# ---------------------------------------------------------------------------
SORT_KEYS = {
    "efficiency": lambda v: (v.efficiency == float("inf"), v.efficiency),
    "size":       lambda v: v.size_bytes,
    "bitrate":    lambda v: v.bitrate_bps,
    "resolution": lambda v: (v.width * v.height),
    "codec":      lambda v: v.codec,
    "name":       lambda v: v.path.name.lower(),
    "fps":        lambda v: v.fps,
    "duration":   lambda v: v.duration_s,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def parse_args():
    p = argparse.ArgumentParser(
        description="Scan a directory for video files, detect corruption, "
                    "and rank by encoding efficiency.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Efficiency metric: bits per pixel per second (bitrate / pixels).
Lower is better. Typical values:
  < 0.05  — excellent (modern codec, well-tuned)
  0.05–0.15 — good
  0.15–0.40 — fair
  0.40–1.0  — poor
  > 1.0   — terrible (likely uncompressed or badly encoded)
"""
    )
    p.add_argument("directory",
                   help="Directory to scan")
    p.add_argument("-r", "--recursive", action="store_true",
                   help="Recurse into subdirectories")
    p.add_argument("--deep", action="store_true",
                   help="Deep corruption scan: decode frames at 3 points "
                        "in the file (slower)")
    p.add_argument("--sort",
                   choices=list(SORT_KEYS.keys()),
                   default="efficiency",
                   help="Sort field (default: efficiency)")
    p.add_argument("--desc", action="store_true",
                   help="Sort descending (default: ascending)")
    p.add_argument("--csv", metavar="FILE",
                   help="Export full results to CSV")
    p.add_argument("--txt", metavar="FILE",
                   help="Export results to plain text file")
    p.add_argument("--no-table", action="store_true",
                   help="Suppress terminal table output")
    p.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 4,
                   help="Parallel probe workers (default: CPU count)")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="Suppress progress messages")
    return p.parse_args()


def main():
    args = parse_args()

    print(BOLD("\n⚙  mediascan"))
    print(DIM("   Checking dependencies..."))
    check_dependencies()

    directory = Path(args.directory).expanduser().resolve()
    if not directory.is_dir():
        print(RED(f"✗ Not a directory: {directory}"))
        sys.exit(1)

    print(DIM(f"   Scanning: {directory}"))
    print(DIM(f"   Recursive: {'yes' if args.recursive else 'no'}"))
    if args.deep:
        print(YELLOW("   Deep scan: enabled (this will be slow)"))

    # Collect
    files = collect_files(directory, args.recursive)
    if not files:
        print(YELLOW(f"\n  No video files found in {directory}"))
        sys.exit(0)

    print(DIM(f"   Found {len(files)} video file(s). Probing with {args.jobs} worker(s)..."))
    print()

    # Probe phase
    results: list[VideoFile] = []
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
                status = (RED("✗ corrupt") if vf.corrupt
                          else GREEN("✓"))
                pct = done * 100 // len(files)
                print(f"\r  [{pct:3d}%] {done}/{len(files)}  {status}  "
                      f"{_trunc(vf.path.name, 50):<50}", end="", flush=True)

    if not args.quiet:
        print()  # newline after progress

    good    = [vf for vf in results if not vf.corrupt]
    corrupt = [vf for vf in results if vf.corrupt]

    # Sort
    sort_fn = SORT_KEYS[args.sort]
    good.sort(key=sort_fn, reverse=args.desc)

    # Output
    print_summary(good, corrupt, len(files))

    if not args.no_table:
        print(BOLD(f"── Valid Files ({len(good)}) ──────────────────────────────"))
        print_table(good, args.sort, args.desc)

    print_corrupt_section(corrupt)

    # Exports
    if args.csv:
        all_sorted = good + corrupt
        export_csv(all_sorted, args.csv)

    if args.txt:
        export_txt(good, corrupt, args.txt, args.sort, args.desc)

    # Exit code: 2 if any corrupt files found
    sys.exit(2 if corrupt else 0)


if __name__ == "__main__":
    main()
