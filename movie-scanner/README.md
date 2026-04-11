# movie-scanner

A terminal-based media library audit tool. Scans a directory of video files to:

1. **Identify corruption** — files ffprobe can't read, missing/broken video streams, zero duration, and optionally decoded-frame corruption
2. **Rank by encoding efficiency** — bits per pixel per second (lower = more efficient), helping you find bloated or poorly encoded files

Zero third-party Python dependencies. Requires `ffmpeg`/`ffprobe`.

## Requirements

- Python 3.10+
- `ffmpeg` / `ffprobe` (checked at startup with install hints if missing)

## Usage

```bash
python3 movie-scanner.py [OPTIONS] <directory>
```

### Options

| Flag | Description |
|------|-------------|
| `-r, --recursive` | Recurse into subdirectories (default: off) |
| `--deep` | Deep corruption scan — decodes frames at 10/50/90% through each file (slower) |
| `--sort <field>` | Sort by: `efficiency`, `size`, `bitrate`, `resolution`, `codec`, `name`, `fps`, `duration` (default: `efficiency`) |
| `--desc` | Sort descending (default: ascending) |
| `--csv <file>` | Export full results to CSV |
| `--txt <file>` | Export results to plain text |
| `--no-table` | Suppress terminal table output |
| `-j, --jobs <n>` | Parallel probe workers (default: CPU count) |
| `-q, --quiet` | Suppress progress output |

### Examples

```bash
# Basic scan of a flat directory
python3 movie-scanner.py /mnt/movies

# Recursive scan with full exports
python3 movie-scanner.py -r /mnt/movies --csv results.csv --txt report.txt

# Deep corruption scan, sort largest first
python3 movie-scanner.py -r /mnt/movies --deep --sort size --desc

# Quiet, limited workers, CSV only
python3 movie-scanner.py -r /mnt/movies -j 4 -q --csv out.csv
```

## Efficiency Metric

**Bits per pixel per second** = `bitrate (bps) / (width × height)`

| Grade | Range | Meaning |
|-------|-------|---------|
| excellent | < 0.05 | Modern codec, well-tuned (H.265/AV1) |
| good | 0.05–0.15 | Solid H.264 encode |
| fair | 0.15–0.40 | Acceptable but room for improvement |
| poor | 0.40–1.0 | Inefficient encode |
| terrible | > 1.0 | Likely uncompressed or badly encoded |

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Clean — no corrupt files |
| `2` | One or more corrupt files found |
