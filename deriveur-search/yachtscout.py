#!/usr/bin/env python3
"""YachtScout — scrape aluminium expedition sailboat listings into a JSON
database for downstream reading by Claude.

Stdlib only. Networking is done by shelling out to curl.

    python3 yachtscout.py                 # normal run (uses cache)
    python3 yachtscout.py --force         # ignore cache, refetch everything
    python3 yachtscout.py --only ayc      # one site
    python3 yachtscout.py --limit 20      # cap detail fetches (for testing)
"""
import argparse
import json
import os
import re
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adapters          # noqa: E402
import criteria          # noqa: E402
import fetch             # noqa: E402
import parse             # noqa: E402
import store             # noqa: E402
import report            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))

if sys.version_info < (3, 7):       # subprocess.run(capture_output=...) is 3.7+
    sys.exit(f"YachtScout needs Python 3.7 or newer (this is "
             f"{sys.version_info.major}.{sys.version_info.minor}). "
             f"Try another interpreter, e.g. PYTHON=python3.11 ./run.sh")


class Context:
    def __init__(self, cfg, args):
        self.cfg = cfg
        self.args = args
        self.http = cfg.get("http", {})
        self.cache_dir = os.path.join(HERE, cfg.get("cache_dir", "cache"))
        self.ttl = 0 if args.force else cfg.get("cache_ttl_hours", 20)
        self.budget = args.limit if args.limit is not None else \
            self.http.get("max_detail_fetches_per_run", 250)
        self.fetch_count = 0
        self.quiet = args.quiet

    def fetch(self, url):
        res = fetch.fetch(url, self.http, self.cache_dir, self.ttl, force=self.args.force)
        if not res.from_cache:
            self.fetch_count += 1
        return res

    def budget_ok(self):
        return self.fetch_count < self.budget

    def log(self, msg):
        if not self.quiet:
            print(msg, flush=True)


def enrich(rec, cfg):
    """Attach derived economics and the criteria assessment."""
    fx = cfg.get("fx_to_usd", {})
    rec["price_usd"] = parse.to_usd(rec.get("price"), fx)
    rec["landed_cost"] = parse.landed_cost_usd(rec.get("price_usd"), cfg)
    if rec.get("loa_m"):
        rec["loa_ft"] = round(rec["loa_m"] * criteria.FT_PER_M, 1)
    rec["assessment"] = criteria.assess(rec, cfg)
    return rec


def main():
    ap = argparse.ArgumentParser(description="Scrape aluminium expedition yachts.")
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--force", action="store_true", help="bypass the HTTP cache")
    ap.add_argument("--only", action="append", help="limit to site id(s)")
    ap.add_argument("--limit", type=int, help="max network detail fetches this run")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as fh:
        cfg = json.load(fh)

    # Keep a real reply address out of the committed config (this repo is
    # public); a polite scraper should still offer one, so take it from the
    # environment and drop the clause entirely when it is unset.
    ua = cfg.get("http", {}).get("user_agent", "")
    contact = os.environ.get("YACHTSCOUT_CONTACT", "").strip()
    if "$YACHTSCOUT_CONTACT" in ua:
        cfg["http"]["user_agent"] = (
            ua.replace("$YACHTSCOUT_CONTACT", contact) if contact
            else re.sub(r"\s*;?\s*contact:\s*\$YACHTSCOUT_CONTACT", "", ua))

    out_dir = os.path.join(HERE, cfg.get("output_dir", "out"))
    state_dir = os.path.join(HERE, cfg.get("state_dir", "state"))
    run_at = fetch.now_iso()
    ctx = Context(cfg, args)

    ctx.log(f"YachtScout run {run_at}")
    all_records, site_reports, sites_ok = [], [], set()

    for site in cfg["sites"]:
        if not site.get("enabled", True):
            continue
        if args.only and site["id"] not in args.only:
            continue
        ctx.log(f"* {site['id']} ({site['adapter']})")
        try:
            mod = adapters.load(site["adapter"])
            records, rep = mod.collect(site, ctx)
        except Exception as exc:                      # one bad site must not kill the run
            rep = {"site": site["id"], "index_ok": False, "index_count": 0,
                   "details_fetched": 0,
                   "errors": [f"adapter crashed: {exc.__class__.__name__}: {exc}"],
                   "traceback": traceback.format_exc()[-1500:]}
            records = []
            ctx.log(f"  !! adapter crashed: {exc}")

        for rec in records:
            rec.setdefault("source_ref", rec.get("url", ""))
            try:
                all_records.append(enrich(rec, cfg))
            except Exception as exc:
                rec["assessment"] = {"hard_pass": False, "score": 0,
                                     "reasons_failed": [f"scoring error: {exc}"],
                                     "warnings": [], "evidence": {}}
                all_records.append(rec)

        if rep.get("index_ok"):
            sites_ok.add(site["id"])
        rep["records"] = len(records)
        site_reports.append(rep)
        ctx.log(f"  {len(records)} listings, {rep.get('details_fetched', 0)} detail fetches"
                + (f", {len(rep['errors'])} problem(s)" if rep.get("errors") else ""))

    all_records = store.link_duplicates(all_records)

    previous = store.load_snapshot(state_dir)
    merged, changes = store.merge_and_diff(previous, all_records, run_at, sites_ok)
    store.save_snapshot(state_dir, merged, run_at)
    store.append_history(state_dir, run_at, changes)

    active = [r for r in merged if r.get("listing_status") != "removed"]
    shortlist = sorted(
        [r for r in active if (r.get("assessment") or {}).get("hard_pass")],
        key=lambda r: (r.get("assessment") or {}).get("ranked_score", 0), reverse=True)

    run_report = {
        "generated_at": run_at,
        "schema_version": store.SCHEMA_VERSION,
        "network_fetches": ctx.fetch_count,
        "sites": site_reports,
        "sites_reachable": sorted(sites_ok),
        "sites_unreachable": sorted(
            r["site"] for r in site_reports if not r.get("index_ok")),
        "counts": {
            "total_tracked": len(merged),
            "active": len(active),
            "shortlist": len(shortlist),
            "new": len(changes["new"]),
            "price_changed": len(changes["price_changed"]),
            "removed": len(changes["removed"]),
            "returned": len(changes["returned"]),
        },
    }

    store.write_json(os.path.join(out_dir, "listings.json"), {
        "schema_version": store.SCHEMA_VERSION, "generated_at": run_at,
        "criteria": cfg["criteria"], "listings": active})
    store.write_json(os.path.join(out_dir, "shortlist.json"), {
        "schema_version": store.SCHEMA_VERSION, "generated_at": run_at,
        "listings": shortlist})
    store.write_json(os.path.join(out_dir, "changes.json"), {
        "schema_version": store.SCHEMA_VERSION, "generated_at": run_at,
        "changes": changes})
    store.write_json(os.path.join(out_dir, "run_report.json"), run_report)
    report.write_brief(os.path.join(out_dir, "brief.md"),
                       run_at, shortlist, active, changes, run_report, cfg)
    report.write_index_html(os.path.join(out_dir, "index.html"), run_at, run_report)

    ctx.log(f"\n{run_report['counts']['active']} active, "
            f"{run_report['counts']['shortlist']} on shortlist, "
            f"{run_report['counts']['new']} new, "
            f"{run_report['counts']['price_changed']} price changes")
    if run_report["sites_unreachable"]:
        ctx.log(f"unreachable: {', '.join(run_report['sites_unreachable'])}")
    ctx.log(f"wrote {out_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
