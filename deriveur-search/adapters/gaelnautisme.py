"""Gaelnautisme (FR). Classic ASP site, clean <table class="caracDetail">.

Spec labels are French; 'deriveur integral' in the Catégorie field is the
single most valuable signal any of these sites gives us.
"""
import re
import adapters
import parse

BASE = "https://gaelnautisme.com/"

LABELS = {
    "marque": "builder", "modele": "model", "modèle": "model",
    "categorie": "category", "catégorie": "category",
    "materiau de construction": "hull_material",
    "matériau de construction": "hull_material",
    "longueur": "loa", "largeur": "beam", "tirant": "draft",
    "tirant d'eau": "draft",
    "nb. cabine": "cabins", "nb. couchages": "berths",
    "nb. salle d'eau": "heads", "annee": "year", "année": "year",
    "visibilite": "location", "visibilité": "location",
    "infos supplementaires": "notes", "infos supplémentaires": "notes",
    "puissance moteur": "engine_power", "marque moteur": "engine_make",
    "nb. heures moteur": "engine_hours", "greement": "rig", "gréement": "rig",
}


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "errors": []}
    listings = []
    seen = set()

    for index_url in site_cfg["index_urls"]:
        res = ctx.fetch(index_url)
        if not res.ok:
            report["errors"].append(f"index {index_url}: {res.error or res.status}")
            continue
        report["index_ok"] = True

        for m in re.finditer(r'href="(annonce-vente-[^"]+?-BATEAU-(\d+)\.asp)"',
                             res.body, re.I):
            href, boat_id = m.group(1), m.group(2)
            if boat_id in seen:
                continue
            seen.add(boat_id)
            url = parse.absolute_url(BASE, href)

            # Cheap pre-filter: the year lives in the URL slug, so we can skip
            # obviously-too-old boats without spending a request on them.
            yr = re.search(r"-(?:OCCASION|O|NEUF)-(\d{4})-", href, re.I)
            slug_year = int(yr.group(1)) if yr else None

            listings.append({
                "_pending_detail": url,
                "source": site_cfg["id"],
                "source_ref": boat_id,
                "url": url,
                "year": slug_year,
                "country": "FR",
            })
        report["index_count"] = len(listings)

    out = []
    cap = adapters.detail_cap(site_cfg)
    for stub in listings:
        if len(out) >= cap:
            report["errors"].append(
                f"per-site cap of {cap} detail fetches reached; the rest are "
                f"cached for the next run")
            break
        if not ctx.budget_ok():
            report["errors"].append("detail-fetch budget exhausted; remaining boats deferred to next run")
            break
        rec = _detail(stub, ctx)
        if rec:
            out.append(rec)
            report["details_fetched"] += 1
    return out, report


def _detail(stub, ctx):
    res = ctx.fetch(stub["_pending_detail"])
    rec = {k: v for k, v in stub.items() if not k.startswith("_")}
    if not res.ok:
        rec["fetch_error"] = res.error or f"HTTP {res.status}"
        return rec

    raw = res.body
    pairs = parse.extract_label_value_pairs(raw)
    specs = {}
    for k, v in pairs.items():
        key = LABELS.get(k.strip().lower())
        if key:
            specs[key] = v
    rec["specs_raw"] = {k: parse.clean(v) for k, v in pairs.items() if v}

    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title_txt = parse.clean(parse.html_to_text(tm.group(1))) if tm else None

    rec["title"] = parse.clean(specs.get("model")) or title_txt
    rec["builder"] = parse.clean(specs.get("builder"))
    rec["model"] = parse.clean(specs.get("model"))
    rec["year"] = parse.parse_year(specs.get("year")) or rec.get("year")
    rec["hull_material"] = parse.clean(specs.get("hull_material"))
    rec["loa_m"] = parse.parse_length(specs.get("loa"))
    rec["beam_m"] = parse.parse_length(specs.get("beam"))
    rec["draft_min_m"], rec["draft_max_m"] = parse.parse_draft(specs.get("draft"))
    rec["cabins"] = parse.parse_int(specs.get("cabins"), 0, 12)
    rec["berths"] = parse.parse_int(specs.get("berths"), 0, 30)
    rec["heads"] = parse.parse_int(specs.get("heads"), 0, 8)
    rec["location"] = parse.clean(specs.get("location"))
    rec["rig"] = parse.clean(specs.get("rig"))

    # Price is not in the spec table (it renders as "NOUS CONTACTER");
    # the <title> carries it, e.g. "... 600000 Euros TTC - GAELNAUTISME".
    price = None
    if title_txt:
        price = parse.parse_price(title_txt, default_currency="EUR")
    if not price or price.get("amount") is None:
        pm = re.search(r'(?is)<div class="price"[^>]*>(.*?)</div>', raw)
        if pm:
            price = parse.parse_price(parse.html_to_text(pm.group(1)), default_currency="EUR")
    rec["price"] = price

    text = parse.html_to_text(raw)
    notes = specs.get("notes") or ""
    body = _description_block(text)
    rec["description"] = parse.clean(" \n".join(x for x in (notes, body) if x))[:6000]
    # 'Catégorie' often literally says "deriveur integral" - keep it prominent.
    rec["category"] = parse.clean(specs.get("category"))
    if rec["category"]:
        rec["description"] = f"[Catégorie: {rec['category']}] " + (rec["description"] or "")
    return rec


def _description_block(text):
    start = text.find("Infos Supplémentaires")
    if start == -1:
        start = text.find("Infos Supplementaires")
    if start == -1:
        return ""
    end = text.find("Disclaimer", start)
    seg = text[start:end if end != -1 else start + 5000]
    return re.sub(r"\n{2,}", "\n", seg)
