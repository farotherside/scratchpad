"""Adapter for sites that currently refuse automated requests.

We do NOT attempt to defeat bot protection. This adapter makes one polite,
identified request per index URL and records the outcome, so each run tells
you plainly whether the site is reachable from this host. If a site starts
answering, swap `adapter` in config.json for a real parser.
"""
import parse


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "errors": [], "blocked": True,
              "note": site_cfg.get("note")}

    for url in site_cfg["index_urls"]:
        res = ctx.fetch(url)
        if res.ok and not _looks_blocked(res.body):
            report["blocked"] = False
            report["index_ok"] = True
            report["errors"].append(
                "SITE IS NOW REACHABLE from this host — write a real adapter for it "
                "and change \"adapter\" in config.json.")
            ctx.log(f"  !! {site_cfg['id']} responded 200 — it is no longer blocked here.")
        else:
            reason = res.error or f"HTTP {res.status}"
            if res.status == 200:
                reason = "HTTP 200 but body is a bot-challenge/interstitial page"
            report["errors"].append(f"{url}: {reason}")
            ctx.log(f"  -- {site_cfg['id']} unreachable: {reason}")
    return [], report


def _looks_blocked(body):
    t = parse.html_to_text(body)[:3000].lower()
    return any(s in t for s in (
        "access denied", "attention required", "just a moment",
        "enable javascript and cookies", "verifying you are human",
        "unusual traffic", "request blocked", "are you a robot",
    ))
