# movie-scanner

A media library audit tool. Scans a directory of video files to:

1. **Find corruption** — unreadable files, missing/broken video streams, zero duration, and (optionally) decoded-frame corruption
2. **Rank encoding efficiency** — codec-normalised bits per pixel so bloated encodes stand out quickly
3. **Generate an HTML report** — self-contained, sortable, filterable, with hover-over explanations of every metric

Zero third-party Python dependencies. Requires `ffmpeg` and `ffprobe`.

## Screenshots

### HTML report
![HTML report](docs/screenshot-html.png)

### Terminal output
![Terminal output](docs/screenshot-terminal.png)

## Requirements

- Python 3.8+
- `ffmpeg` / `ffprobe`

### macOS / Homebrew

```bash
brew install ffmpeg python@3.11
python3.11 movie-scanner.py /path/to/movies
```

### Linux

```bash
sudo apt install ffmpeg          # Debian / Ubuntu
sudo dnf install ffmpeg          # Fedora
sudo pacman -S ffmpeg            # Arch
python3 movie-scanner.py /path/to/movies
```

## Quick start

```bash
# Scan a folder and open the HTML report
python3 movie-scanner.py /mnt/movies --html
open report.html      # macOS
xdg-open report.html  # Linux
```

## Usage

```bash
python3 movie-scanner.py [OPTIONS] <directory>
```

### Options

| Flag | Description |
|------|-------------|
| `-r, --recursive` | Recurse into subdirectories |
| `--html [FILE]` | Write a self-contained HTML report. Omit `FILE` to auto-name `report.html` in the scanned directory. |
| `--deep` | Deep corruption scan — decodes frames at 10/50/90% through each file (slower) |
| `--sort <field>` | Sort by `efficiency`, `size`, `bitrate`, `resolution`, `codec`, `name`, `fps`, `duration` |
| `--desc` | Sort descending |
| `--csv <file>` | Export full results to CSV |
| `--txt <file>` | Export results to plain text |
| `--no-table` | Suppress terminal table output |
| `-j, --jobs <n>` | Parallel probe workers |
| `-q, --quiet` | Suppress progress output |

### Examples

```bash
# Scan a flat directory, open HTML report automatically
python3 movie-scanner.py /mnt/movies --html

# Recursive scan, save HTML to a specific path
python3 movie-scanner.py -r /mnt/movies --html ~/reports/movies.html

# Deep corruption scan, sort by size (largest first)
python3 movie-scanner.py -r /mnt/movies --deep --sort size --desc

# Quiet scan, CSV only
python3 movie-scanner.py -r /mnt/movies -j 4 -q --csv out.csv

# Terminal table only, no HTML
python3 movie-scanner.py /mnt/movies
```

## Understanding the HTML report

The report is a single `.html` file you can open in any browser. Every column header and grade badge has a **hover-over tooltip** explaining what it means.

### Key metrics

| Term | Plain English |
|------|--------------|
| **NormEff** | Codec-normalised efficiency: bits per pixel, adjusted for how efficient the codec is. H.264 is the baseline (1.0×). Lower = better encoded. Hover the column header for the full explanation. |
| **RawEff** | Raw bits per pixel — no codec adjustment. Useful for comparing files with the same codec. |
| **Factor** | How much more efficient this codec is vs H.264. AV1 = 2.5×, HEVC = 1.8×, VP9 = 1.4×, H.264 = 1.0×. |
| **Grade** | Percentile rank within *your* library. Hover the grade badge for a plain-English description. |
| **Pctile** | Raw percentile (0 = most efficient, 100 = most bloated). |

### Grade bands

| Grade | Percentile | What it means |
|-------|-----------|---------------|
| **excellent** | Bottom 20% | Best-encoded fifth of your library |
| **good** | 20–45% | Well-encoded, efficient use of space |
| **fair** | 45–70% | Average — some re-encode potential |
| **poor** | 70–88% | Bloated — file is larger than it needs to be |
| **terrible** | Top 12% | Very bloated — good re-encode candidate |

Grades are **relative to your library**, not fixed absolute thresholds.

### File hover cards

Hover any filename in the table to see:
- Full file path
- File size
- Resolution, bitrate, duration, FPS
- Video codec and audio tracks
- Container format

## Notes

- AppleDouble sidecar files (`._Movie.mkv`) are ignored automatically.
- Exit code `0` = no corrupt files found.
- Exit code `2` = one or more corrupt files found.
- Grades compare files against each other — a library of all HEVC files will have grades spread across excellent → terrible just like a mixed library.
