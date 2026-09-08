"""AYC Yacht Broker (FR, Wix). Listing URLs sit in the index HTML directly.

Wix pages carry the whole site nav inline, including a list of every OTHER
boat and its price, so the body text must be cut free of that before anything
is parsed out of it -- otherwise every listing inherits its neighbours' prices.

Specs live in a 'Caractéristiques Techniques' block as 'Label: value' lines.
The later 'Inventaire' block repeats labels like 'Modèle' for the *engine*, so
spec scanning stops at 'Inventaire'.
"""
import re
import parse

BASE = "https://www.ayc-yachtbroker.com"

NAV_END = "use tab to navigate through the menu items"
SPEC_START = re.compile(r"(?i)caract[ée]ristiques?\s+techniques?")
SPEC_END = re.compile(r"(?i)^\s*(inventaire|situation administrative)\s*$", re.M)

FIELD_MAP = {
    "longueur de coque": "loa", "longueur hors tout": "loa", "longueur": "loa",
    "loa": "loa", "longueur a la flottaison": "lwl",
    "bau maxi": "beam", "bau": "beam", "largeur": "beam",
    "tirant d'eau": "draft", "tirant d eau": "draft",
    "construction": "hull_material", "materiau": "hull_material",
    "coque": "hull_material",
    "architecte": "designer", "chantier": "builder", "constructeur": "builder",
    "annee": "year", "annee de construction": "year",
    "greement": "rig", "voilure": "rig",
    "cabines": "cabins", "nombre de cabines": "cabins",
    "couchages": "berths", "salle d'eau": "heads", "salles d'eau": "heads",
    "localisation": "location", "situation": "location", "lieu": "location",
    "pavillon": "flag", "deplacement": "displacement", "lest": "ballast",
    "prix": "price", "certification c.e.": "ce_category",
}

SOLD = re.compile(r"(?i)\b(deja vendu|déjà vendu|vendu|sold|sous compromis|under offer)\b")


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "skipped_sold": 0, "errors": []}
    stubs, seen = [], set()

    for index_url in site_cfg["index_urls"]:
        res = ctx.fetch(index_url)
        if not res.ok:
            report["errors"].append(f"index {index_url}: {res.error or res.status}")
            continue
        report["index_ok"] = True

        for m in re.finditer(
                r'href="((?:https://www\.ayc-yachtbroker\.com)?/voiliers/[^"#?]+)"',
                res.body, re.I):
            url = parse.absolute_url(BASE, m.group(1))
            slug = url.rstrip("/").rsplit("/", 1)[-1]
            if slug in seen or slug == "voiliers":
                continue
            seen.add(slug)
            stubs.append({"source": site_cfg["id"], "source_ref": slug,
                          "url": url, "country": "FR", "_slug": slug})
    report["index_count"] = len(stubs)

    out = []
    for stub in stubs:
        if not ctx.budget_ok():
            report["errors"].append("detail-fetch budget exhausted; remaining boats deferred")
            break
        rec = _detail(stub, ctx)
        if rec is None:
            report["skipped_sold"] += 1
            continue
        out.append(rec)
        report["details_fetched"] += 1
    return out, report


def _strip_nav(text):
    """Remove the Wix site nav (which lists every other boat, with prices)."""
    lines = [l.rstrip() for l in text.split("\n")]
    start = 0
    for i, l in enumerate(lines[:80]):
        if NAV_END in l.lower():
            start = i + 1
            break
    else:
        # Fallback: drop the run of 'Name - year - price' cross-links.
        xlink = re.compile(r"^\s*.{3,45}\s-\s.*\d[\d\s]{2,}\s*(€|\$|EUR|USD)", re.I)
        lines = [l for l in lines if not xlink.match(l)]
    body = [l for l in lines[start:] if l.strip()]
    # Trim the footer / contact form.
    stop = re.compile(r"(?i)^(nous contacter|contactez|newsletter|nom\b|pr[ée]nom\b|"
                      r"adresse e-mail|©|tous droits|mentions l[ée]gales)")
    cut = len(body)
    for i, l in enumerate(body):
        if stop.match(l.strip()):
            cut = i
            break
    return "\n".join(body[:cut])


def _spec_pairs(body):
    """Parse 'Label: value' lines from the technical block only."""
    m = SPEC_START.search(body)
    seg = body[m.end():] if m else body
    e = SPEC_END.search(seg)
    if e:
        seg = seg[:e.start()]
    seg = seg[:6000]

    specs = {}
    for line in seg.split("\n"):
        line = line.strip()
        mm = re.match(r"^([A-Za-zÀ-ÿ'’.\-/ ]{3,36})\s*:\s*(.{1,120})$", line)
        if not mm:
            continue
        key = re.sub(r"[.\s]+$", "", mm.group(1).strip().lower())
        key = key.replace("’", "'")
        import unicodedata
        flat = "".join(c for c in unicodedata.normalize("NFKD", key)
                       if not unicodedata.combining(c))
        field = FIELD_MAP.get(key) or FIELD_MAP.get(flat)
        if field:
            specs.setdefault(field, mm.group(2).strip())
    return specs


def _detail(stub, ctx):
    slug = stub.pop("_slug")
    rec = dict(stub)
    res = ctx.fetch(stub["url"])
    if not res.ok:
        rec["fetch_error"] = res.error or f"HTTP {res.status}"
        rec["title"] = slug.replace("-", " ").upper()
        return rec

    raw_text = parse.html_to_text(res.body)
    body = _strip_nav(raw_text)

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", res.body)
    heading = parse.clean(parse.html_to_text(h1.group(1))) if h1 else None
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", res.body)
    title_txt = parse.clean(parse.html_to_text(tm.group(1))) if tm else None
    if title_txt:
        title_txt = re.split(r"\s*\|\s*", title_txt)[0]
    title = heading or title_txt or slug.replace("-", " ").upper()

    # Skip boats the broker has already marked sold.
    if SOLD.search(title) or SOLD.search(body[:300]):
        return None

    specs = _spec_pairs(body)
    rec["specs_raw"] = {k: parse.clean(v) for k, v in specs.items()}
    rec["title"] = title
    rec["builder"] = parse.clean(specs.get("builder"))
    rec["model"] = parse.clean(specs.get("model"))
    rec["designer"] = parse.clean(specs.get("designer"))
    rec["year"] = (parse.parse_year(specs.get("year")) or parse.parse_year(title)
                   or parse.parse_year(slug))
    rec["hull_material"] = parse.clean(specs.get("hull_material"))
    rec["loa_m"] = parse.parse_length(specs.get("loa"))
    rec["beam_m"] = parse.parse_length(specs.get("beam"))
    rec["draft_min_m"], rec["draft_max_m"] = parse.parse_draft(specs.get("draft"))
    rec["cabins"] = parse.parse_int(specs.get("cabins"), 0, 12) or _cabins_from_text(body)
    rec["berths"] = parse.parse_int(specs.get("berths"), 0, 30)
    rec["heads"] = parse.parse_int(specs.get("heads"), 0, 8) or _heads_from_text(body)
    rec["location"] = parse.clean(specs.get("location")) or parse.clean(specs.get("flag"))
    rec["rig"] = parse.clean(specs.get("rig"))

    # Price: the page title carries a formatted price ("... - 380 000€ TTC").
    # The URL slug does NOT -- 'patago45-380000' would read as 45,380,000.
    price = parse.parse_price(title, default_currency="EUR")
    if not price or price.get("amount") is None:
        price = parse.parse_price(specs.get("price"), default_currency="EUR")
    rec["price"] = price

    rec["description"] = parse.clean(body)[:9000]
    return rec


def _cabins_from_text(body):
    m = re.search(r"(?i)version\s+(\d)\s+cabines?|(\d)\s+cabines?\b", body)
    if m:
        v = int(m.group(1) or m.group(2))
        return v if 1 <= v <= 8 else None
    return None


def _heads_from_text(body):
    n = len(re.findall(r"(?i)salle d'eau\s+(avant|arriere|arrière|babord|bâbord|tribord)", body))
    return n if n else None
