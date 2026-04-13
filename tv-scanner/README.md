# tv-scanner

Scan a local TV library and compare it against [TVmaze](https://www.tvmaze.com/api) and optionally TheTVDB to find missing or extra episodes.

## Requirements

- Python 3.9+
- `requests`

### Quick setup

```bash
cd tv-scanner
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

### macOS note

The system Python on older macOS releases can be awkward, especially with SSL and package installs. If you run into that, use Homebrew Python:

```bash
brew install python@3.11
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

No API key is needed for TVmaze-only mode. TheTVDB mode requires an API key file.

## Usage

```bash
python tv_scanner.py /media/external/TV [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output text\|json\|csv\|html` | `text` | Output format |
| `--missing-only` | off | Only show shows with gaps |
| `--workers N` | `4` | Parallel API workers |
| `--outfile PATH` | stdout | Write report to file |
| `--source tvmaze\|thetvdb\|both` | `tvmaze` | Metadata source |
| `--thetvdb-apikey PATH` | unset | TheTVDB API key file |
| `--no-color` | off | Disable ANSI color output |

### Examples

```bash
# Basic scan, print to terminal
python tv_scanner.py /media/usb/TV

# Only show problems, save to file
python tv_scanner.py /media/usb/TV --missing-only --outfile report.txt

# Machine-readable JSON
python tv_scanner.py /media/usb/TV --output json --outfile report.json

# CSV for spreadsheet import
python tv_scanner.py /media/usb/TV --output csv --outfile report.csv

# HTML report
python tv_scanner.py /media/usb/TV --output html --outfile report.html

# Query both TVmaze and TheTVDB, choose best match per show
python tv_scanner.py /media/usb/TV --source both --thetvdb-apikey ~/.config/thetvdb.key
```

## Directory Structure Expected

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

Season folder names are flexible, `Season 1`, `S01`, `s1`, `Season_01`, `1` all parse correctly.
Episode filenames are parsed by common patterns like `S01E02`, `S01E01-E03`, `S01E01E02`, and `1x02`.

## Notes

- Only aired episodes are checked by default.
- Specials / non-regular episode types are excluded.
- AppleDouble sidecar files like `._Episode.mkv` are ignored.
- If a show name does not match well, the matched title shown in the report helps you rename the folder for better results.
