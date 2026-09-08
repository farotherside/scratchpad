# YachtScout

Scrapes aluminium expedition sailboat listings into a JSON database, detects
changes between runs, and publishes the result to a web-visible directory for
a downstream Claude to read.

**No dependencies.** Python 3.7+ standard library, shelling out to `curl`.
Nothing to `pip install`, no root needed — built for a shared shell account.

## Install

    git clone https://github.com/farotherside/scratchpad.git
    cd scratchpad/deriveur-search
    python3 -V && command -v curl        # need Python >= 3.7 and curl
    python3 yachtscout.py --limit 5      # smoke test, ~5 requests

If the host's `python3` is older than 3.7, look for `python3.8`/`python3.11` on
the box and use `PYTHON=python3.11 ./run.sh`.

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

| source | what it adds |
|---|---|
| `gaelnautisme` | French aluminium specialist; the richest single source of *dériveur intégral* boats |
| `ayc` | French broker; Ovni/Alubat, Meta, Patago, Chatam |
| `devalk` | Large EU brokerage; index cards allow pre-filtering, detail pages expose `dataLayer` + JSON-LD |
| `owenclarke` | UK explorer/high-latitude specialist |
| `apolloduck` | International classifieds, brokerage *and* private sales |
| `hisseetoh` | French sailing community — owner sales that never reach a brokerage |
| `mail` | Saved-search alert emails (disabled until configured) |

**Blocked:** `yachtworld`, `yachtfocus` (Cloudflare) and `boat24` (WAF). The
block is client fingerprinting, not IP reputation — an automated browser is
refused too, while a hand-driven one works. They stay in `config.json` using
the `generic_blocked` adapter, which makes one polite request per run and
records the outcome, so `run_report.json` tells you if anything changes.
**No attempt is made to defeat bot protection.** Recover those sources through
their saved-search email alerts instead — see below.

## Email alerts (recovering the blocked sites)

Create saved searches on YachtWorld/YachtFocus/boat24 in your browser and point
the alerts at a mailbox this can read. That is the sanctioned route, and it is
faster than scraping: you hear when a boat lists, not up to a day later.

Set `"enabled": true` on the `mail` site in `config.json`, then either read a
local mailbox:

    "maildir": "~/Maildir/.Boats/new"     (or "mbox": "~/mail/boats")

or IMAP, with credentials only ever in the environment:

    export YACHTSCOUT_IMAP_HOST=imap.example.com
    export YACHTSCOUT_IMAP_USER=you@example.com
    export YACHTSCOUT_IMAP_PASS='an app-specific password'
    export YACHTSCOUT_IMAP_FOLDER=Boats

The mailbox is opened **read-only** and never modified. Alert formats vary by
sender, so extraction is best-effort: records are tagged `untrusted_source` and
`needs_manual_open`, carry the link and whatever price/year/length appears near
it, and are meant as a prompt to go look — not as a full spec sheet.

## Crawl budgets

Sites with no usable server-side filter are crawled a slice at a time via
`max_details`. Because fetches are cached for 20 h, successive runs reach
further in and steady state costs only genuinely new listings. De Valk is the
opposite case: its index cards carry material, size, year and price, so a
typical run pre-filters ~500 boats away and fetches under ten pages.

Adding a source: write `adapters/<name>.py` exposing
`collect(site_cfg, ctx) -> (listings, report)` and add it to `config.json`.
Adapters only extract; all filtering and scoring happens centrally in
`criteria.assess()`.

## Being a good citizen

Identifies itself in the User-Agent, honours `robots.txt`, waits 4–6 s between
requests to the same host, and caches for 20 h so repeat runs cost the target
sites almost nothing. A steady-state daily run is a handful of requests.
