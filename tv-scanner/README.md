# tv-scanner

Scan a local TV library and compare it against [TVmaze](https://www.tvmaze.com/api) to find missing or extra episodes.

## Requirements

```bash
pip install requests
```

No API key needed — TVmaze's public API is free and unauthenticated.

## Usage

```bash
python tv_scanner.py /media/external/TV [options]
```

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--output text\|json\|csv` | `text` | Output format |
| `--missing-only` | off | Only show shows with gaps |
| `--workers N` | `4` | Parallel API workers |
| `--outfile PATH` | stdout | Write report to file |

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
```

## Directory Structure Expected

```
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
Episode filenames are parsed by common patterns: `S01E02`, `1x02`, etc.

## How It Works

1. **Scan** — walks the library root, detects show/season/episode structure
2. **Lookup** — searches TVmaze for each show name, fetches full episode list
3. **Compare** — checks which aired episodes are present locally
4. **Report** — prints summary with missing/extra episodes by season

### Notes

- Only **aired** episodes are checked (no airdate = skipped)
- Specials / non-regular episode types are excluded
- TVmaze's public API allows ~20 req/s; the scanner is polite at 4 workers + 200ms delay
- If a show name doesn't match well, the `[TVmaze: …]` label in text output shows what was matched — you can rename the folder to improve matching

## Sample Output

```
TV Library Report — 3 shows scanned
────────────────────────────────────────────────────────────
  ✓ Complete:  2
  ✗ Issues:    1

► Breaking Bad  (Ended)
    Local: 62 episodes  |  TVmaze (aired): 62 episodes
    ✓ All aired episodes present

► Game of Thrones  (Ended)
    Local: 71 episodes  |  TVmaze (aired): 73 episodes
    ✗ Missing S04: S04E09, S04E10

► The Wire  (Ended)
    Local: 60 episodes  |  TVmaze (aired): 60 episodes
    ✓ All aired episodes present
```
