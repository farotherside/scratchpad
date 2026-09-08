"""De Valk (NL/FR/BE/ES/TR) — one of the largest European brokerages and the
richest single source of aluminium expedition yachts now that YachtWorld is
closed to us.

Two things make this adapter cheap: the index cards already carry material,
dimensions, year and price, so we pre-filter before spending a request on a
detail page; and every detail page embeds a `dataLayer.push({...})` object plus
schema.org JSON-LD, so the important fields are machine-readable rather than
scraped out of prose.
"""
import json
import re

import adapters
import parse

BASE = "https://www.devalk.nl"
INDEX = BASE + "/en/Yachts-for-sale.html"

# Which De Valk office holds the boat -> where it physically is.
OFFICE_COUNTRY = {
    "hindeloopen": "NL", "loosdrecht": "NL", "monnickendam": "NL",
    "sneek": "NL", "amsterdam": "NL", "antibes": "FR", "france": "FR",
    "palma": "ES", "spain": "ES", "mallorca": "ES", "belgium": "BE",
    "antwerp": "BE", "turkey": "TR", "bodrum": "TR", "greece": "GR",
    "uk": "GB", "england": "GB",
}


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "prefiltered_out": 0, "errors": []}
    max_pages = site_cfg.get("max_index_pages", 12)
    seen, candidates = set(), []

    for page in range(1, max_pages + 1):
        url = INDEX if page == 1 else f"{INDEX}?page={page}"
        res = ctx.fetch(url)
        if not res.ok:
            report["errors"].append(f"index page {page}: {res.error or res.status}")
            break
        report["index_ok"] = True

        cards = _index_cards(res.body)
        if not cards:
            break
        new_here = 0
        for card in cards:
            if card["id"] in seen:
                continue
            seen.add(card["id"])
            new_here += 1
            if _card_worth_fetching(card, ctx.cfg):
                candidates.append(card)
            else:
                report["prefiltered_out"] += 1
        if new_here == 0:
            break        # pagination exhausted / looping

    report["index_count"] = len(seen)

    out = []
    cap = adapters.detail_cap(site_cfg)
    for card in candidates:
        if len(out) >= cap:
            report["errors"].append(
                f"per-site cap of {cap} detail fetches reached; the rest are "
                f"cached for the next run")
            break
        if not ctx.budget_ok():
            report["errors"].append("detail-fetch budget exhausted; remaining boats deferred")
            break
        rec = _detail(card, site_cfg, ctx)
        if rec:
            out.append(rec)
            report["details_fetched"] += 1
    return out, report


def _index_cards(body):
    """Each card links to /en/yachtbrokerage/<id>/<NAME>.html and reads
    'NAME / Built 2026 / Material GRP / Dimensions 8.30 x 2.40 x 0.70 (m) /
    Asking price EUR 89.000' once flattened."""
    cards = []
    for m in re.finditer(
            r'(?is)href="([^"]*?/en/yachtbrokerage/(\d+)/[^"]*?)"[^>]*>(.{0,2000}?)'
            r'(?=<a\b|\Z)', body):
        href, boat_id, inner = m.group(1), m.group(2), m.group(3)
        flat = re.sub(r"\s*\n\s*", " / ", parse.html_to_text(inner)).strip(" /")
        segs = [x.strip() for x in flat.split("/") if x.strip()]
        card = {"id": boat_id, "url": parse.absolute_url(BASE, href),
                "text": flat, "segments": segs}
        card["year"] = parse.parse_year(_seg(segs, "built"))
        card["material"] = _seg(segs, "material")
        dims = _seg(segs, "dimensions")
        card["loa_m"] = None
        if dims:
            dm = re.search(r"(\d{1,3}[.,]\d{1,2})\s*x", dims)
            if dm:
                card["loa_m"] = float(dm.group(1).replace(",", "."))
        card["price_text"] = _seg(segs, "asking price") or _seg(segs, "price")
        cards.append(card)
    return cards


def _seg(segments, label):
    """Pull 'Material GRP' -> 'GRP'. Dimensions span two '/' segments
    ('Dimensions 8.30 x 2.40 x 0.70 (m)'), so a prefix match is enough."""
    for s in segments:
        low = s.lower()
        if low.startswith(label):
            val = s[len(label):].strip(" :\t")
            if val:
                return val
    return None


def _card_worth_fetching(card, cfg):
    """Cheap gate so we don't pull thousands of GRP motorboat pages.

    Deliberately permissive: anything unknown is fetched, because the real
    filtering belongs in criteria.assess() where it can be explained.
    """
    mat = (card.get("material") or "").lower()
    if mat and not re.search(r"alu", mat):
        return False
    loa = card.get("loa_m")
    if loa:
        crit = cfg["criteria"]["length_ft"]
        tol = crit.get("tolerance_ft", 0) + 4      # cards round; be generous
        ft = loa * 3.280839895
        if ft < crit["min"] - tol or ft > crit["max"] + tol:
            return False
    year = card.get("year")
    if year:
        import datetime
        if year < datetime.date.today().year - cfg["criteria"]["year"]["max_age_years"] - 3:
            return False
    return True


def _datalayer(raw):
    """Pull the analytics object; it is single-quoted JS, not JSON."""
    m = re.search(r"(?s)dataLayer\.push\(\{(.*?)\}\)", raw)
    if not m:
        return {}
    out = {}
    for km, vm in re.findall(r"'([^']+)'\s*:\s*'([^']*)'", m.group(1)):
        out[km.strip().lower()] = vm.strip()
    return out


def _jsonld(raw):
    for m in re.finditer(r'(?is)<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', raw):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for node in (d if isinstance(d, list) else [d]):
            if isinstance(node, dict) and node.get("@type") == "Product":
                return node
    return {}


def _detail(card, site_cfg, ctx):
    res = ctx.fetch(card["url"])
    rec = {"source": site_cfg["id"], "source_ref": card["id"], "url": card["url"]}
    if not res.ok:
        rec["fetch_error"] = res.error or f"HTTP {res.status}"
        rec["title"] = card["text"][:60]
        return rec

    raw = res.body
    dl = _datalayer(raw)
    ld = _jsonld(raw)
    props = {}
    for p in (ld.get("additionalProperty") or []):
        if isinstance(p, dict) and p.get("name"):
            props[str(p["name"]).strip().lower()] = str(p.get("value", "")).strip()

    # De Valk's index mixes sail and motor; dataLayer says which ('S' / 'M').
    boattype = (dl.get("boattype") or "").strip().upper()
    if boattype and boattype != "S":
        return None

    text = parse.html_to_text(raw)
    rec["specs_raw"] = {**{f"dl_{k}": v for k, v in dl.items()}, **props}

    rec["title"] = parse.clean(ld.get("name")) or parse.clean(
        f"{dl.get('brand', '')} {dl.get('model', '')}") or card["text"][:60]
    rec["builder"] = parse.clean(dl.get("brand"))
    rec["model"] = parse.clean(dl.get("model"))
    rec["year"] = parse.parse_year(dl.get("built")) or card.get("year")
    rec["hull_material"] = parse.clean(dl.get("material")) or card.get("material")

    # dataLayer 'length' is centimetres.
    loa = None
    if dl.get("length"):
        try:
            loa = round(int(re.sub(r"\D", "", dl["length"])) / 100.0, 2)
        except (ValueError, TypeError):
            loa = None
    rec["loa_m"] = loa or parse.parse_length(props.get("length")) or card.get("loa_m")
    rec["beam_m"] = parse.parse_length(props.get("beam"))
    rec["draft_min_m"], rec["draft_max_m"] = parse.parse_draft(
        props.get("draft") or _spec_line(text, r"draft"))
    rec["cabins"] = parse.parse_int(_spec_line(text, r"cabins?"), 0, 12)
    rec["berths"] = parse.parse_int(_spec_line(text, r"berths?"), 0, 30)
    rec["heads"] = parse.parse_int(_spec_line(text, r"(?:heads|toilets?)"), 0, 8)
    rec["rig"] = parse.clean(_spec_line(text, r"(?:rig|rigging)"))

    office = (dl.get("sales office") or "").lower()
    rec["location"] = parse.clean(dl.get("sales office"))
    rec["country"] = next((c for k, c in OFFICE_COUNTRY.items() if k in office), None)

    price_txt = card.get("price_text") or ""
    if dl.get("asking price"):
        price_txt = f"{dl['asking price']} {price_txt}"
    rec["price"] = parse.parse_price(price_txt, default_currency="EUR")

    if (dl.get("status") or "").lower() not in ("", "for sale"):
        rec["status"] = dl["status"].lower().replace(" ", "_")

    rec["description"] = parse.clean(
        (ld.get("description") or "") + "\n" + _body(text))[:9000]
    return rec


def _spec_line(text, label):
    m = re.search(rf"(?im)^\s*{label}\s*[:\t ]\s*(.{{1,60}})$", text)
    return m.group(1).strip() if m else None


def _body(text):
    m = re.search(r"(?i)\n\s*(general|accommodation|specification)", text)
    seg = text[m.start():] if m else text
    stop = re.search(r"(?i)\n\s*(disclaimer|this (?:information|specification)|"
                     r"cookie|newsletter|de valk's story)", seg)
    return seg[:stop.start()] if stop else seg[:9000]
