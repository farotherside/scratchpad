"""Apollo Duck (UK) — large international classifieds board carrying both
brokerage and private sales, including aluminium expedition boats that never
reach the big aggregators.

Listing URLs are /boat/<slug>/<id>; paging is ?next=<offset>&limit=<n>.
Detail pages are plain HTML with a specification list.
"""
import re

import adapters
import parse

BASE = "https://www.apolloduck.com"
LINK = re.compile(r'href="([^"]*?/boat/[a-z0-9\-]+/(\d+))"', re.I)
# Cards read like "60' Trad 1994 John Wh... £59,950" -- enough length and year
# to skip most boats before spending a request on their detail page.
CARD = re.compile(
    r'(?is)href="([^"]*?/boat/[a-z0-9\-]+/(\d+))"[^>]*>(.{0,2400}?)(?=href="[^"]*?/boat/|\Z)')


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "prefiltered_out": 0, "errors": []}
    per_page = site_cfg.get("page_size", 100)
    max_pages = site_cfg.get("max_index_pages", 4)
    seen, stubs = set(), []

    for base_url in site_cfg["index_urls"]:
        for page in range(max_pages):
            sep = "&" if "?" in base_url else "?"
            url = f"{base_url}{sep}limit={per_page}"
            if page:
                url += f"&next={page * per_page}"
            res = ctx.fetch(url)
            if not res.ok:
                report["errors"].append(f"index {url}: {res.error or res.status}")
                break
            report["index_ok"] = True

            found = 0
            for m in CARD.finditer(res.body):
                href, boat_id, inner = m.group(1), m.group(2), m.group(3)
                if boat_id in seen:
                    continue
                seen.add(boat_id)
                found += 1
                # The summary ("60' Trad 1994 John White ... £59,950") lives
                # in the thumbnail's alt attribute, which tag-stripping drops.
                alts = " ".join(a for a in re.findall(r'alt="([^"]{4,120})"', inner))
                card = re.sub(r"\s+", " ",
                              parse.clean(alts + " " + parse.html_to_text(inner)) or "").strip()
                if not _card_worth_fetching(card, ctx.cfg):
                    report["prefiltered_out"] += 1
                    continue
                stubs.append({"source": site_cfg["id"], "source_ref": boat_id,
                              "url": parse.absolute_url(BASE, href),
                              "_card": card})
            if found == 0:
                break
    report["index_count"] = len(stubs)

    out = []
    cap = adapters.detail_cap(site_cfg)
    for stub in stubs:
        if len(out) >= cap:
            report["errors"].append(
                f"per-site cap of {cap} detail fetches reached; the rest are "
                f"cached for the next run")
            break
        if not ctx.budget_ok():
            report["errors"].append("detail-fetch budget exhausted; remaining boats deferred")
            break
        rec = _detail(stub, ctx)
        if rec:
            out.append(rec)
            report["details_fetched"] += 1
    return out, report


def _card_worth_fetching(card, cfg):
    """Length and year come free on the index; material does not, so anything
    unknown is still fetched and judged centrally."""
    crit = cfg["criteria"]
    m = re.search(r"(\d{2,3})\s*['\u2019]", card)          # 60' -> feet
    if not m:
        m2 = re.search(r"(\d{1,2}[.,]\d{1,2})\s*m\b", card)
        ft = float(m2.group(1).replace(",", ".")) * 3.280839895 if m2 else None
    else:
        ft = float(m.group(1))
    if ft is not None:
        tol = crit["length_ft"].get("tolerance_ft", 0) + 2
        if ft < crit["length_ft"]["min"] - tol or ft > crit["length_ft"]["max"] + tol:
            return False
    ym = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", card)
    if ym:
        import datetime
        if int(ym.group(1)) < datetime.date.today().year - crit["year"]["max_age_years"] - 2:
            return False
    return True


LABELS = {
    "length over all": "loa", "length overall": "loa", "length": "loa", "loa": "loa",
    "constructed": "year", "no. of engines": "engine_count",
    "engine model": "engine", "hull type": "hull_type",
    "beam": "beam", "draft": "draft", "draught": "draft",
    "hull material": "hull_material", "material": "hull_material",
    "construction": "hull_material",
    "year": "year", "year built": "year", "built": "year",
    "cabins": "cabins", "berths": "berths", "heads": "heads",
    "rig": "rig", "rig type": "rig", "keel": "keel", "keel type": "keel",
    "location": "location", "lying": "location", "country": "country_name",
    "price": "price", "asking price": "price", "designer": "designer",
    "builder": "builder", "manufacturer": "builder", "engine": "engine",
    "displacement": "displacement",
}


def _detail(stub, ctx):
    rec = {k: v for k, v in stub.items() if not k.startswith("_")}
    res = ctx.fetch(stub["url"])
    if not res.ok:
        rec["fetch_error"] = res.error or f"HTTP {res.status}"
        return rec

    raw = res.body
    full = parse.html_to_text(raw)

    # Apollo Duck wraps every page in a ~320-line global country nav and
    # follows it with a "similar boats" rail. Both are full of other boats'
    # names and prices, so the record is cut out of the middle first.
    lines = [l.strip() for l in full.split("\n") if l.strip()]
    start = next((i for i, l in enumerate(lines)
                  if re.match(r"(?i)^(for sale|for rent|wanted)\s*:", l)), None)
    if start is None:
        start = next((i for i, l in enumerate(lines) if l.lower() == "description"), 0)
    end = next((i for i, l in enumerate(lines[start:], start)
                if re.match(r"(?i)^(location map|similar|you may also)", l)), len(lines))
    block = lines[start:end]
    text = "\n".join(block)

    # Specs render as 'Label:' then the value on the next line.
    specs = {}
    for k, v in parse.pairs_from_lines(text, set(LABELS)).items():
        f = LABELS.get(k)
        if f:
            specs.setdefault(f, v)
    for line in block:
        m = re.match(r"^([A-Za-z][A-Za-z ./]{2,28})\s*:\s*(.{1,90})$", line)
        if m:
            f = LABELS.get(parse._label_key(m.group(1)))
            if f:
                specs.setdefault(f, m.group(2).strip())
    rec["specs_raw"] = {k: parse.clean(v) for k, v in specs.items() if v}

    title = None
    if block and re.match(r"(?i)^(for sale|for rent|wanted)\s*:", block[0]):
        title = parse.clean(re.sub(r"(?i)^(for sale|for rent|wanted)\s*:\s*", "", block[0]))
    if not title:
        h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", raw)
        title = parse.clean(parse.html_to_text(h1.group(1))) if h1 else None
    rec["title"] = title

    # The breadcrumb ("UK House Boats > Static Houseboat For Sale") says what
    # kind of craft this actually is -- worth keeping as evidence.
    category = next((l for l in block[:6] if ">" in l and len(l) < 120), None)
    rec["category"] = parse.clean(category)

    rec["builder"] = parse.clean(specs.get("builder"))
    rec["model"] = parse.clean(specs.get("model")) or title
    rec["year"] = parse.parse_year(specs.get("year")) or parse.parse_year(title or "")
    rec["hull_material"] = parse.clean(specs.get("hull_material"))
    rec["loa_m"] = parse.parse_length(specs.get("loa"))
    rec["beam_m"] = parse.parse_length(specs.get("beam"))
    rec["draft_min_m"], rec["draft_max_m"] = parse.parse_draft(specs.get("draft"))
    rec["cabins"] = parse.parse_int(specs.get("cabins"), 0, 12)
    rec["berths"] = parse.parse_int(specs.get("berths"), 0, 30)
    rec["heads"] = parse.parse_int(specs.get("heads"), 0, 8)
    rec["rig"] = parse.clean(specs.get("rig"))
    rec["location"] = parse.clean(specs.get("location"))
    rec["country"] = _country(specs.get("country_name") or specs.get("location"))
    if (specs.get("status") or "").strip().lower() not in ("", "available"):
        rec["status"] = specs["status"].strip().lower().replace(" ", "_")

    price_txt = specs.get("price")
    if not price_txt:
        price_txt = next((l for l in block[:5] if re.search(r"[£€$]\s?[\d,]{3,}", l)), None)
    rec["price"] = parse.parse_price(price_txt, default_currency="GBP")

    keel = parse.clean(specs.get("keel"))
    prefix = ""
    if keel:
        prefix += f"[Keel: {keel}] "
    if rec.get("category"):
        prefix += f"[Category: {rec['category']}] "
    rec["description"] = parse.clean(prefix + text)[:9000]
    return rec


_C = {"united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB",
      "wales": "GB", "ireland": "IE", "france": "FR", "netherlands": "NL",
      "holland": "NL", "belgium": "BE", "germany": "DE", "spain": "ES",
      "portugal": "PT", "italy": "IT", "greece": "GR", "croatia": "HR",
      "turkey": "TR", "sweden": "SE", "norway": "NO", "denmark": "DK",
      "finland": "FI", "usa": "US", "united states": "US", "canada": "CA",
      "caribbean": "AG", "antigua": "AG", "grenada": "GD", "martinique": "MQ",
      "australia": "AU", "new zealand": "NZ", "thailand": "TH",
      "south africa": "ZA", "malaysia": "MY", "panama": "PA", "chile": "CL"}


def _country(s):
    if not s:
        return None
    low = s.lower()
    for k, v in _C.items():
        if k in low:
            return v
    return None


def _body(text):
    drop = re.compile(r"(?i)^(apollo ?duck|advertise|sign in|register|cookie|"
                      r"privacy|terms|contact the seller|similar boats|"
                      r"boats for sale|search|menu|home)\b")
    keep = [l for l in text.split("\n") if l.strip() and not drop.match(l.strip())]
    return "\n".join(keep[:400])
