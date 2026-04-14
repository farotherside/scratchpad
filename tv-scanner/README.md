# tv-scanner

Scan a local TV library and compare it against [TVmaze](https://www.tvmaze.com/api) and optionally TheTVDB to find missing or extra episodes. Produces a self-contained HTML report with hover-over explanations, season file lists, and episode name tooltips.

## Screenshots

### HTML report
![HTML report](docs/screenshot-html.png)

### Terminal output
![Terminal output](docs/screenshot-terminal.png)

## Requirements

- Python 3.9+
- `requests`

### Quick setup

```bash
cd tv-scanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### macOS note

The system Python on older macOS releases can be awkward with SSL and package installs. Use Homebrew Python if you hit issues:

```bash
brew install python@3.11
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

No API key is needed for TVmaze-only mode. TheTVDB mode requires an API key file.

## Quick start

```bash
# Scan a TV library and open the HTML report
python3 tv-scanner.py /media/external/TV --html
open report.html      # macOS
xdg-open report.html  # Linux
```

## Usage

```bash
python3 tv-scanner.py /path/to/tv/root [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output text\|json\|csv\|html` | `text` | Primary output format |
| `--html [FILE]` | — | Also write an HTML report. Omit `FILE` to auto-name `report.html` in the scanned directory. |
| `--missing-only` | off | Only show shows with gaps |
| `--workers N` | `4` | Parallel API workers |
| `--outfile PATH` | stdout | Write primary output to file |
| `--source tvmaze\|thetvdb\|both` | `tvmaze` | Metadata source |
| `--thetvdb-apikey PATH` | unset | Path to file containing TheTVDB API key |
| `--no-color` | off | Disable ANSI colour output |

### Examples

```bash
# Basic scan — print to terminal and save an HTML report
python3 tv-scanner.py /media/usb/TV --html

# Only show problems, HTML saved to a specific path
python3 tv-scanner.py /media/usb/TV --missing-only --html ~/reports/tv.html

# Machine-readable JSON
python3 tv-scanner.py /media/usb/TV --output json --outfile report.json

# CSV for spreadsheet import
python3 tv-scanner.py /media/usb/TV --output csv --outfile report.csv

# Query both TVmaze and TheTVDB, pick best match per show
python3 tv-scanner.py /media/usb/TV --source both --thetvdb-apikey ~/.config/thetvdb.key
```

## Understanding the HTML report

The report is a single `.html` file you can open in any browser. Every **column header**, **status badge**, **source badge**, and **episode tag** has a hover-over tooltip.

### Columns

| Column | Plain English |
|--------|--------------|
| **Show** | Your folder name. Hover for matched title, TVmaze ID, and a season-by-season file list with full paths. |
| **Status** | Whether the show is still airing. Hover the badge for a description. |
| **Source** | Where the episode list came from (TVmaze or TheTVDB). Hover for details. |
| **Local** | How many episode files are on disk for this show. |
| **Remote** | How many aired episodes the metadata source knows about (specials excluded). |
| **Missing** | How many aired episodes are not found on disk. |
| **Detail** | Exact season/episode numbers of gaps. Hover an episode tag to see the episode name and air date. |

### Filtering

- **Search box** — type to filter by show name in real time
- **Issues only** checkbox — hide complete shows, show only those with missing episodes or errors

## Directory structure expected

```text
TV Root/
├── Breaking Bad/
│   ├── Season 1/
│   │   ├── Breaking.Bad.S01E01.mkv
│   │   └── …
│   └── Season 2/
│       └── …
├── The Wire/
│   └── …
└── …
```

Season folder names are flexible — `Season 1`, `S01`, `s1`, `Season_01`, `1` all parse correctly.
Episode filenames are parsed by common patterns like `S01E02`, `S01E01-E03`, `S01E01E02`, and `1x02`.

## Notes

- Only aired episodes are checked by default.
- Specials / non-regular episode types are excluded.
- AppleDouble sidecar files like `._Episode.mkv` are ignored.
- If a show name doesn't match well, the matched title shown in the report (and in the hover card) helps you rename the folder for better results.
