# YachtScout

Scrapes aluminium expedition sailboat listings into a JSON database, detects
changes between runs, and publishes the result to a web-visible directory for
a downstream Claude to read.

**No dependencies.** Python 3.6+ standard library, shelling out to `curl`.
Nothing to `pip install`, no root needed — built for a shared shell account.

## Install

    git clone https://github.com/farotherside/scratchpad.git
    cd scratchpad/deriveur-search
    python3 yachtscout.py --limit 5      # smoke test, ~5 requests

## Run

    ./run.sh                                    # run + publish
    PUBLISH_DIR=~/public_html/boats ./run.sh    # choose the web directory

    python3 yachtscout.py --only ayc      # one source
    python3 yachtscout.py --force         # ignore the HTTP cache
    python3 yachtscout.py --limit 20      # cap network fetches

## Identify yourself

The User-Agent carries a contact address so site owners can reach you. It is
not committed (this repo is public) — set it in the environment:

    export YACHTSCOUT_CONTACT="you@example.com"

Unset, the clause is dropped and the scraper still identifies itself by name.

## Cron

Daily at 06:15, staggered off the hour to be a polite neighbour:

    15 6 * * * YACHTSCOUT_CONTACT=you@example.com PUBLISH_DIR=$HOME/public_html/boats $HOME/deriveur-search/run.sh

`run.sh` takes a lock, so an overrunning job never doubles up.

## Output (written to `out/`, copied to `PUBLISH_DIR`)

| file | what it is |
|---|---|
| `brief.md` | readable summary of this run |
| `changes.json` | new / price-changed / removed since last run |
| `shortlist.json` | boats passing every hard filter, ranked |
| `listings.json` | full database including complete broker text |
| `run_report.json` | per-source health |
| `index.html` | landing page linking the above |

State lives in `state/snapshot.json` (previous run) and `state/history.jsonl`
(append-only event log). Delete `state/` to start over; delete `cache/` to
force fresh fetches.

## Tuning the search

Everything is in `config.json` — length band, max age, price range, plate
thickness thresholds, import tax rates, FX, and the site list. The interesting
one:

    "length_ft": { "min": 47, "max": 72, "tolerance_ft": 1.5 }

Several strong candidates (Ovni 435/450, Boréal 44, Crozet 46, Patago 45) sit
just under 47 ft. Lowering `min` to 44 surfaces them; the interior scoring and
`cabins`/`heads` fields then do the real filtering.

The matching vocabulary lives in `criteria.py` as annotated regex tables
(`BEACHABLE`, `ICE_AND_PLATING`, `INSULATION`, `RIG`, `INTERIOR`), in French,
English, Dutch and German. Add a phrase and its weight to teach it a new signal.

## Sources

Working: `gaelnautisme`, `ayc`, `owenclarke`.

Blocked from this machine: `yachtworld`, `yachtfocus` (Cloudflare) and
`boat24` (WAF 403, though its robots.txt permits the path). They stay in
`config.json` using the `generic_blocked` adapter, which makes one polite
request per run and records the outcome — so `run_report.json` tells you if
your host's IP fares better. It does not attempt to defeat bot protection.

Adding a source: write `adapters/<name>.py` exposing
`collect(site_cfg, ctx) -> (listings, report)` and add it to `config.json`.
Adapters only extract; all filtering and scoring happens centrally in
`criteria.assess()`.

## Being a good citizen

Identifies itself in the User-Agent, honours `robots.txt`, waits 4–6 s between
requests to the same host, and caches for 20 h so repeat runs cost the target
sites almost nothing. A steady-state daily run is a handful of requests.
