"""Hisse et Oh (FR) — a French sailing community whose classifieds carry
private sales that never reach a brokerage. Worth watching precisely because
an owner-sold aluminium centreboarder is exactly the boat that doesn't get
listed on YachtWorld.

The board mixes boats with charter offers, crew wanted, gear and 'looking for'
ads, so the index is filtered by intent before we spend requests on details.
"""
import re

import adapters
import parse

BASE = "https://www.hisse-et-oh.com"

# Ads that are not a boat being sold.
NOT_FOR_SALE = re.compile(
    r"(?i)\b(recherche|cherche|location|louer|a louer|embarquement|equipier|"
    r"équipier|coproprietaire|copropriétaire|stage|ecole|école|croisiere "
    r"accompagnee|place de port|gardiennage|emploi|demande)\b")
GEAR = re.compile(
    r"(?i)\b(annexe|moteur hors|hors.?bord|silent.?block|voile d|grand.?voile|"
    r"genois|génois|enrouleur|guindeau|pilote|radeau|survie|vhf|radar|"
    r"electronique|électronique|remorque|winch|accastillage|batterie|"
    r"panneau solaire|combinaison|cirage)\b")

LABELS = {
    "modele": "model", "modèle": "model", "type": "hull_type",
    "annee": "year", "année": "year",
    "constructeur": "builder", "architecte": "designer",
    "longueur": "loa", "long.": "loa", "long": "loa",
    "largeur": "beam", "larg.": "beam", "larg": "beam",
    "lieu/emplacement": "location", "emplacement": "location",
    "tirant": "draft", "poids": "displacement", "prix de vente": "price",
    "tirant d'eau": "draft", "tirant d’eau": "draft",
    "materiau": "hull_material", "matériau": "hull_material",
    "coque": "hull_material", "greement": "rig", "gréement": "rig",
    "cabines": "cabins", "couchages": "berths",
    "prix": "price", "lieu": "location", "localisation": "location",
    "deplacement": "displacement", "déplacement": "displacement",
}


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "skipped_not_for_sale": 0, "errors": []}
    max_pages = site_cfg.get("max_index_pages", 5)
    seen, stubs = set(), []

    for base_url in site_cfg["index_urls"]:
        for page in range(1, max_pages + 1):
            sep = "&" if "?" in base_url else "?"
            url = base_url if page == 1 else f"{base_url}{sep}page={page}"
            res = ctx.fetch(url)
            if not res.ok:
                report["errors"].append(f"index {url}: {res.error or res.status}")
                break
            report["index_ok"] = True

            found = 0
            for m in re.finditer(r'href="(/av/([a-z0-9\-]+))"', res.body, re.I):
                href, slug = m.group(1), m.group(2)
                if slug in seen or slug in ("sailboats", "motorboats"):
                    continue
                seen.add(slug)
                found += 1
                if NOT_FOR_SALE.search(slug) or GEAR.search(slug):
                    report["skipped_not_for_sale"] += 1
                    continue
                stubs.append({"source": site_cfg["id"], "source_ref": slug,
                              "url": parse.absolute_url(BASE, href),
                              "country": "FR"})
            if found == 0:
                break
    report["index_count"] = len(seen)

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


def _detail(stub, ctx):
    rec = dict(stub)
    res = ctx.fetch(stub["url"])
    if not res.ok:
        rec["fetch_error"] = res.error or f"HTTP {res.status}"
        return rec

    raw = res.body
    text = parse.html_to_text(raw)

    h1 = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", raw)
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = (parse.clean(parse.html_to_text(h1.group(1))) if h1 else None) or \
            (parse.clean(parse.html_to_text(tm.group(1))) if tm else None)
    rec["title"] = title

    if title and (NOT_FOR_SALE.search(title) or GEAR.search(title)):
        return None

    specs = {}
    pairs = parse.pairs_from_lines(text, set(LABELS))
    for k, v in pairs.items():
        f = LABELS.get(k)
        if f:
            specs.setdefault(f, v)
    for m in re.finditer(r"(?im)^([A-Za-zÀ-ÿ'’. ]{3,26})\s*:\s*(.{1,80})$", text):
        f = LABELS.get(parse._label_key(m.group(1)).replace("’", "'"))
        if f:
            specs.setdefault(f, m.group(2).strip())
    rec["specs_raw"] = {k: parse.clean(v) for k, v in specs.items()}

    rec["builder"] = parse.clean(specs.get("builder"))
    rec["model"] = parse.clean(specs.get("model"))
    rec["year"] = parse.parse_year(specs.get("year")) or parse.parse_year(title)
    rec["hull_material"] = parse.clean(specs.get("hull_material"))
    rec["loa_m"] = parse.parse_length(specs.get("loa"))
    rec["beam_m"] = parse.parse_length(specs.get("beam"))
    rec["draft_min_m"], rec["draft_max_m"] = parse.parse_draft(specs.get("draft"))
    rec["cabins"] = parse.parse_int(specs.get("cabins"), 0, 12)
    rec["berths"] = parse.parse_int(specs.get("berths"), 0, 30)
    rec["rig"] = parse.clean(specs.get("rig"))
    rec["location"] = parse.clean(specs.get("location"))
    # "Prix de vente" is a heading whose following line is an insurance link,
    # so a labelled value is only trusted when it actually parses as money.
    price = parse.parse_price(specs.get("price"), default_currency="EUR")
    if not price or price.get("amount") is None:
        # Pages carry decoy amounts ("Tarif journalier 0 EUR", insurance
        # widgets), so take the largest plausible figure, not the first.
        best = 0
        for pm in re.finditer(r"([\d][\d\u00a0\u202f .,]{2,})\s*(?:EUR|\u20ac)", text):
            v = parse._to_float(pm.group(1))
            if v and 3000 <= v <= 20_000_000:
                best = max(best, v)
        if best:
            price = parse.parse_price(f"{int(best)} EUR", default_currency="EUR")
    if not price or price.get("amount") is None:
        price = parse.parse_price(title, default_currency="EUR")
    rec["price"] = price

    # Location lines end with an ISO country code: "21300, Kilada, Argolida · GR"
    cm = re.search(r"·\s*([A-Z]{2})\b", rec.get("location") or text)
    if cm:
        rec["country"] = cm.group(1)
    rec["listing_kind"] = "private"      # community board, mostly owner sales
    rec["description"] = parse.clean(_body(text))[:9000]
    return rec


def _body(text):
    drop = re.compile(r"(?i)^(voile|bateaux moteurs|la taverne|support|univers|"
                      r"news des pros|articles|annonces|communaut|devenez premium|"
                      r"heo blogs|wikiboats|heo clubs|concours|connexion|"
                      r"location de bateaux|bourse d|emploi du nautisme|"
                      r"puces nautiques|hisse et oh)")
    keep = [l for l in text.split("\n") if l.strip() and not drop.match(l.strip())]
    return "\n".join(keep[:300])
