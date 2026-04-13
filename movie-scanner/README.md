# movie-scanner

A terminal-based media library audit tool. Scans a directory of video files to:

1. identify corruption, including unreadable files, missing/broken video streams, zero duration, and optionally decoded-frame corruption
2. rank files by encoding efficiency, using codec-normalised bits per pixel so bloated encodes stand out quickly
3. optionally compute VMAF against a known reference file when ffmpeg is built with `libvmaf`

Zero third-party Python dependencies. Requires `ffmpeg` and `ffprobe`.

## Requirements

- Python 3.8+
- `ffmpeg` / `ffprobe`
- optional: ffmpeg with `libvmaf` for `--vmaf-reference`

### macOS / Homebrew

```bash
brew install ffmpeg python@3.11
python3.11 movie-scanner.py /path/to/movies
```

## Usage

```bash
python3 movie-scanner.py [OPTIONS] <directory>
```

### Options

| Flag | Description |
|------|-------------|
| `-r, --recursive` | Recurse into subdirectories |
| `--deep` | Deep corruption scan, decodes frames at 10/50/90% through each file |
| `--sort <field>` | Sort by `efficiency`, `vmaf`, `size`, `bitrate`, `resolution`, `codec`, `name`, `fps`, `duration` |
| `--desc` | Sort descending |
| `--csv <file>` | Export full results to CSV |
| `--txt <file>` | Export results to plain text |
| `--html <file>` | Export results to a self-contained HTML report |
| `--vmaf-reference <file>` | Compute VMAF for each scanned file against a reference file |
| `--no-table` | Suppress terminal table output |
| `-j, --jobs <n>` | Parallel probe workers |
| `-q, --quiet` | Suppress progress output |

### Examples

```bash
# Basic scan of a flat directory
python3 movie-scanner.py /mnt/movies

# Recursive scan with exports
python3 movie-scanner.py -r /mnt/movies --csv results.csv --txt report.txt --html report.html

# Deep corruption scan, sort largest first
python3 movie-scanner.py -r /mnt/movies --deep --sort size --desc

# Quiet, limited workers, CSV only
python3 movie-scanner.py -r /mnt/movies -j 4 -q --csv out.csv

# Compare all encodes against a known source/reference using VMAF
python3 movie-scanner.py -r /mnt/movies --vmaf-reference /mnt/reference/Movie.mkv --sort vmaf --desc
```

## Notes

- AppleDouble sidecar files like `._Movie.mkv` are ignored.
- VMAF is full-reference, so `--vmaf-reference` compares each scanned file against the file you provide.
- `--vmaf-reference` requires an ffmpeg build with the `libvmaf` filter enabled.
- Exit code `0` means no corrupt files were found.
- Exit code `2` means one or more corrupt files were found.
- Grades are percentile-based relative to the scanned library, not fixed absolute quality labels.
