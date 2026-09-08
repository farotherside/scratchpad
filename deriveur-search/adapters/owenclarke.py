"""Owen Clarke Design brokerage (UK). Specialist aluminium/explorer broker.

Listings render as repeating cards:
    <name>  /  <designer>  /  Launch: <year>  /  Lying: <place>  /  <price|SOLD>
so we parse the flattened text with a line-window scanner anchored on 'Launch:'.
"""
import re
import parse

BASE = "https://www.owenclarkedesign.com"

TYPE_WORDS = (r"sloop|cutter|ketch|yawl|schooner|catamaran|trimaran|yacht|"
              r"cruiser|explorer|motor ?sailer|racer")


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "errors": []}
    out = []

    for index_url in site_cfg["index_urls"]:
        res = ctx.fetch(index_url)
        if not res.ok:
            report["errors"].append(f"index {index_url}: {res.error or res.status}")
            continue
        report["index_ok"] = True

        text = parse.html_to_text(res.body)
        links = _listing_links(res.body)
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        # Card titles look like "19.5m Aluminium Explorer Yacht"; they bound
        # each card, so evidence from one boat never leaks into the next.
        title_re = re.compile(rf"(?i)^\d{{1,2}}(?:[.,]\d{{1,2}})?\s*m\b.*({TYPE_WORDS})")
        title_idx = [j for j, l in enumerate(lines) if title_re.match(l)]

        for i, line in enumerate(lines):
            if not re.match(r"(?i)^launch\s*[:.]", line):
                continue
            title = None
            designer = None
            for back in (1, 2, 3):
                if i - back < 0:
                    break
                cand = lines[i - back]
                if re.search(rf"(?i)\d+\.?\d*\s*m\b.*({TYPE_WORDS})|({TYPE_WORDS})", cand) \
                        and len(cand) < 90:
                    title = cand
                    designer = lines[i - back + 1] if back > 1 else designer
                    break
            if not title:
                continue

            year = parse.parse_year(line)
            location, price_txt, status = None, None, "for_sale"
            for fwd in range(1, 5):
                if i + fwd >= len(lines):
                    break
                nxt = lines[i + fwd]
                if re.match(r"(?i)^lying\s*[:.]", nxt):
                    location = parse.clean(re.sub(r"(?i)^lying\s*[:.]\s*", "", nxt))
                elif re.match(r"(?i)^(sold|under offer|sale agreed)\b", nxt):
                    status = nxt.strip().lower().replace(" ", "_")
                elif re.search(r"[€£$]\s?\d|\bPOA\b|price on application", nxt, re.I):
                    price_txt = nxt
                elif re.match(r"(?i)^price reduced", nxt):
                    status = "price_reduced"
                elif location is None and len(nxt) < 60 and not re.match(r"(?i)^launch", nxt):
                    location = parse.clean(nxt)

            if status == "sold":
                continue    # already gone; don't carry it into the database

            loa_m = None
            lm = re.match(r"\s*(\d{1,2}(?:[.,]\d{1,2})?)\s*m\b", title)
            if lm:
                loa_m = float(lm.group(1).replace(",", "."))

            # Bound this card: from its own title to just before the next one.
            here = max((j for j in title_idx if i - 4 <= j <= i), default=None)
            nxt_title = next((j for j in title_idx if j > i), len(lines))
            card_start = here if here is not None else max(0, i - 2)
            card_end = min(nxt_title, i + 6)
            card = lines[card_start:card_end]

            ref = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:60]
            rec = {
                "source": site_cfg["id"],
                "source_ref": ref,
                "url": links.get(ref) or _best_link(links, title) or index_url,
                "title": parse.clean(title),
                "builder": parse.clean(designer),
                "model": parse.clean(designer),
                "year": year,
                "loa_m": loa_m,
                "location": location,
                "country": _country(location),
                "hull_material": _material(title),
                "status": status,
                "price": parse.parse_price(price_txt, default_currency="EUR") if price_txt else None,
                "description": parse.clean(" | ".join(card)),
                "specs_raw": {"card": " | ".join(card)},
            }
            out.append(rec)

        report["index_count"] = len(out)

    # Detail pages add hull thickness / insulation detail the card omits.
    enriched = []
    for rec in out:
        if rec["url"] and rec["url"] not in site_cfg["index_urls"] and ctx.budget_ok():
            d = ctx.fetch(rec["url"])
            if d.ok:
                body = parse.html_to_text(d.body)
                rec["description"] = (rec.get("description") or "") + "\n" + body[:5000]
                report["details_fetched"] += 1
        enriched.append(rec)
    return enriched, report


def _listing_links(raw):
    links = {}
    for m in re.finditer(r'href="([^"]*owenclarkedesign\.com/+[^"]+|/[^":]+)"', raw, re.I):
        href = m.group(1)
        url = parse.absolute_url(BASE, href.replace("com//", "com/"))
        slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
        links[re.sub(r"[^a-z0-9]+", "-", slug).strip("-")] = url
    return links


def _best_link(links, title):
    t = re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    best, best_score = None, 0
    for slug, url in links.items():
        s = sum(1 for w in t if w and w in slug)
        if s > best_score:
            best, best_score = url, s
    return best if best_score >= 2 else None


def _material(title):
    """The card title states the material; the surrounding page is an aluminium
    brokerage, so text-sniffing would call every steel boat aluminium."""
    t = title.lower()
    for pat, name in ((r"alumini?um", "aluminium"), (r"\bsteel\b", "steel"),
                      (r"\bcarbon\b", "carbon"), (r"\bgrp\b|composite|fibreglass", "grp"),
                      (r"\bwood\b|timber", "wood")):
        if re.search(pat, t):
            return name
    return None


_COUNTRY_HINTS = {
    "france": "FR", "brittany": "FR", "bretagne": "FR", "mediterranean": "FR",
    "uk": "GB", "england": "GB", "scotland": "GB", "wales": "GB",
    "netherlands": "NL", "holland": "NL", "belgium": "BE", "germany": "DE",
    "spain": "ES", "canaries": "ES", "canary": "ES", "mallorca": "ES",
    "portugal": "PT", "italy": "IT", "greece": "GR", "croatia": "HR",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI",
    "usa": "US", "united states": "US", "canada": "CA", "caribbean": "AG",
    "antigua": "AG", "martinique": "MQ", "guadeloupe": "GP",
    "australia": "AU", "new zealand": "NZ", "polynesia": "PF", "tahiti": "PF",
    "south africa": "ZA", "chile": "CL", "argentina": "AR", "patagonia": "CL",
}


def _country(location):
    if not location:
        return None
    low = location.lower()
    for k, v in _COUNTRY_HINTS.items():
        if k in low:
            return v
    return None
