"""Scoring a listing against the search criteria.

Design principle: this module does NOT try to be the final judge. It applies
hard filters only where the data is machine-reliable (material, length, year,
price), and for everything subjective -- beachability, ice-capable plating,
insulation, interior layout -- it emits SCORES PLUS THE EVIDENCE SNIPPETS that
produced them. The downstream Claude reads the evidence and makes the call.
"""
import datetime
import re
import unicodedata

FT_PER_M = 3.280839895


def _norm(s):
    """Lowercase and strip accents so 'dériveur' matches 'deriveur'."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s).lower())
    return "".join(c for c in s if not unicodedata.combining(c))


# --------------------------------------------------------------- lexicons ---
# (pattern, weight, human label). Positive weight = good, negative = bad.

BEACHABLE = [
    # Unambiguous: integral centreboarder, ballast in the hull, flat bottom.
    (r"derive(ur)?\s+integral|deriveur-integral|integral centre-?board|integral dagger",  40, "integral centreboarder (dériveur intégral)"),
    (r"\bbeachable\b|can be beached|dries out upright|beaches upright|droogval",          40, "explicitly described as beachable"),
    (r"echouage|s'echouer|peut echouer|talonnage|sabots? d'echouage|beaching legs",       30, "designed for drying out (échouage)"),
    (r"fond plat|flat[- ]bottom|flat bottomed",                                           25, "flat-bottomed hull"),
    (r"safran(s)? relevable(s)?|lifting rudder|kick-?up rudder|retractable rudder",       25, "lifting/kick-up rudder(s)"),
    (r"quille relevable|lifting keel|swing keel|centre-?board|centreboard|dagger-?board|"
     r"zwaard|hubkiel|schwenkkiel|orza abatible",                                         20, "lifting/swing keel or centreboard"),
    (r"tirant d'eau variable|variable draft|variable draught",                            15, "variable draft"),
    # Strong hull-form hints from expedition aluminium yards.
    (r"\bovni\b|\balubat\b|\bboreal\b|\ballures\b|\bgarcia\b(?!.*motor)|\bmeta\b|"
     r"\bfutuna\b|\bkm yacht|bestevaer|\bcigale\b|\bjfa\b|\bpatago\b|dix design|"
     r"\bstrongall\b|\bsetag\b|\bagba\b|\bpogo\b",                                        15, "yard known for beachable aluminium centreboarders"),
    # Disqualifiers.
    (r"\bbulbe?\b|bulb keel|keel bulb|torpedo keel|lest en bulbe",                       -45, "BULB on keel — blocks beaching"),
    (r"quille pendulaire|canting keel|kielpendel",                                       -35, "canting keel"),
    (r"quillard|fixed keel|quille fixe|deep fin keel|vaste kiel|fin keel",               -30, "fixed fin keel"),
    (r"bi-?quille|bilge keel|twin keel|dubbele kiel|kielschwein",                        -12, "bilge/twin keels (less preferred)"),
]

ICE_AND_PLATING = [
    (r"renforc\w* glace|ice[- ]reinforc|ice class|classe glace|ice[- ]strengthen",        35, "ice-reinforced"),
    (r"haute[s]? latitude|high latitude|polar|antarctic|arctic|spitzberg|svalbard|"
     r"groenland|greenland|patagonia|patagonie|grand froid",                              20, "high-latitude / polar use"),
    (r"expedition|exploration|explorer yacht|voilier d'exploration",                      10, "expedition/explorer boat"),
]

INSULATION = [
    (r"\bisole\b|\bisolee\b|isolation (thermique|complete|integrale)|"
     r"\binsulated\b|full insulation|thermal insulation|geisoleerd|isoliert|"
     r"armaflex|mousse polyurethane|polyurethane foam|spray[- ]?foam|liege projete",      35, "insulated hull"),
    (r"non isole|not insulated|sans isolation|uninsulated",                              -40, "explicitly NOT insulated"),
]

RIG = [
    (r"\bcotre\b|\bcutter\b",            30, "cutter"),
    # A permanently rigged staysail forward of the genoa is what makes a cutter,
    # and French listings often describe the sail plan without naming the rig.
    (r"trinquette|staysail|inner forestay|etai largable|bastaque",
                                          22, "staysail/inner forestay (cutter-rigged)"),
    (r"\bsloop\b|\bsloup\b",             18, "sloop"),
    (r"\bketch\b",                        6, "ketch"),
    (r"\byawl\b",                         6, "yawl"),
    (r"goelette|schooner|schoener",       4, "schooner"),
]

INTERIOR = [
    (r"cabine de douche|douche separee|separate shower|shower stall|shower cubicle|"
     r"douche independante|aparte douche",                                                25, "separate shower stall"),
    (r"wet head|douche dans les toilettes|shower over (the )?(toilet|heads)",            -20, "wet head (no separate shower)"),
    (r"lave-?linge|machine a laver|washing machine|washer[/ ]?dryer|wasmachine|"
     r"waschmaschine|laundry",                                                            15, "laundry machine"),
    (r"\bwatermaker\b|dessalinisateur|desalinator|osmoseur",                               8, "watermaker"),
    (r"generat(eur|or)|groupe electrogene",                                                5, "generator"),
    (r"chauffage|heating|webasto|eberspacher|espar|diesel heater|hydronic",               10, "cabin heating"),
    (r"cockpit central|center cockpit|centre cockpit",                                     5, "centre cockpit (aft cabin privacy)"),
    (r"doghouse|rouf|pilot ?house|timonerie|wheelhouse",                                   8, "pilothouse/doghouse (cold-weather shelter)"),
]

ALUMINIUM = r"alumini?um|alu\b|aluminio|aluminiu|aluminium|\balu\.|aluminum"
NOT_ALUMINIUM = r"\b(grp|frp|fibreglass|fiberglass|polyester|composite|carbon|" \
                r"\bsteel\b|acier|stahl|staal|\bwood\b|\bbois\b|ferro-?cement|" \
                r"sandwich|epoxy glass|strip.?planking?|cold.?moul\w*|contreplaque)"

# Every sailboat has an aluminium mast, boom and hatches, so a bare mention of
# the word proves nothing. Infer an aluminium HULL only when the word sits next
# to a hull term, or the boat is from a yard that builds nothing else.
_HULL_WORD = (r"coque|hull|bord[eé]|carene|carène|construction|materiau|"
              r"mat[eé]riau|material|romp|rumpf|casco|structure|plating|"
              r"chantier|built in|constructed")
_ALU_YARDS = (r"\balubat\b|\bovni\b|\bboreal\b|\ballures\b|\bgarcia\b|"
              r"\bmeta\b|\bfutuna\b|km yacht|bestevaer|\bcigale\b|\bjfa\b|"
              r"\bpatago\b|\bstrongall\b|\balliage\b|\bagba\b|\bpassoa\b|"
              r"\bchatam\b|\bcrozet\b|van de stadt|berckemeyer|aluyacht|"
              r"\bwesthinder\b|\bhermine\b|dix design|\bnordia\b")


def aluminium_hull_evidence(text):
    """Return (bool, snippet). Aluminium must be tied to the hull, not the rig."""
    for m in re.finditer(ALUMINIUM, text):
        window = text[max(0, m.start() - 90):m.end() + 90]
        if re.search(_HULL_WORD, window):
            return True, "…" + re.sub(r"\s+", " ", window).strip() + "…"
    m = re.search(_ALU_YARDS, text)
    if m:
        return True, f"built by {m.group(0)}, a yard that builds aluminium hulls"
    return False, None


def _scan(text, rules):
    """Apply a rule list; return (score, [evidence dicts])."""
    score, evidence, seen = 0, [], set()
    for pattern, weight, label in rules:
        m = re.search(pattern, text)
        if not m and label not in seen:
            continue
        if label in seen:
            continue
        seen.add(label)
        score += weight
        start = max(0, m.start() - 70)
        snippet = re.sub(r"\s+", " ", text[start:m.end() + 70]).strip()
        evidence.append({"label": label, "weight": weight,
                         "matched": m.group(0)[:60], "context": "…" + snippet + "…"})
    return score, evidence


# Words that mean the millimetre figure describes the HULL itself...
_HULL_CTX = (r"bord[eé]|coque|carene|hull|plating|tole|oeuvres vives|topsides|"
             r"fond de coque|fond plat|rumpf|romp|epaisseur de (la )?coque|"
             r"hull thickness|hull plate|plate thickness|bouchain")
# ...and words that mean it describes something else bolted to the hull.
_NOT_HULL_CTX = (r"timonerie|rouf|roof|doghouse|wheelhouse|pilot ?house|abri|"
                 r"superstructure|pont\b|deck|cockpit|hublot|window|vitre|plexi|"
                 r"altuglas|davier|taquet|mat\b|bome|boom|safran|derive\b|"
                 r"reservoir|tank|cloison|bulkhead|balcon|chandelier|"
                 r"guindeau|winch|cable|corde|cordage|amarre|chaine")


def extract_plate_mm(text):
    """Find aluminium thicknesses, classified by what they actually describe.

    Brokerage copy routinely quotes the doghouse or deck thickness ("timonerie
    aluminium 5 mm") right next to hull details. Treating that as hull plating
    rejects excellent boats, so each hit is tagged 'hull', 'other' or
    'ambiguous' and only 'hull' hits drive the hard filter.
    """
    hits = []
    for m in re.finditer(r"(\d{1,2}(?:[.,]\d)?)\s*mm", text):
        val = float(m.group(1).replace(",", "."))
        if not (2 <= val <= 30):
            continue
        ctx = text[max(0, m.start() - 110):m.end() + 70]
        near = text[max(0, m.start() - 45):m.end() + 35]
        is_alu = bool(re.search(r"alumini?um|\balu\b|5083|5086|alliage", ctx))
        hull = bool(re.search(_HULL_CTX, ctx))
        other = bool(re.search(_NOT_HULL_CTX, near))
        if not (is_alu or hull):
            continue
        kind = "other" if other and not hull else ("hull" if hull else "ambiguous")
        hits.append({"mm": val, "kind": kind,
                     "context": "…" + re.sub(r"\s+", " ", ctx).strip() + "…"})
    return hits


def parse_material(mat):
    for pat, name in ((r"\bsteel\b|acier|stahl|staal", "steel"),
                      (r"grp|frp|fibreglass|fiberglass|polyester|composite", "GRP"),
                      (r"\bwood\b|\bbois\b|timber|epoxy", "wood"),
                      (r"ferro", "ferrocement"), (r"carbon", "carbon")):
        if re.search(pat, mat):
            return name
    return mat[:24] or "unknown"


def _rig_from(text):
    for pattern, _w, label in RIG:
        if re.search(pattern, text):
            return label
    return None


def assess(listing, cfg):
    """Score one listing. Mutates nothing; returns the assessment dict."""
    crit = cfg["criteria"]
    blob_parts = [
        listing.get("title") or "", listing.get("model") or "",
        listing.get("builder") or "", listing.get("description") or "",
    ]
    for k, v in (listing.get("specs_raw") or {}).items():
        blob_parts.append(f"{k}: {v}")
    text = _norm("\n".join(blob_parts))

    reasons_fail, warnings = [], []

    # ---- Hard filter: hull material ---------------------------------------
    # Aluminium is the hardest of the hard criteria, so it must be positively
    # established -- from the material field or from the listing text. Absence
    # of any mention is treated as failure, not as "unknown": otherwise every
    # barge and GRP boat with an unlabelled spec sheet lands on the shortlist.
    mat = _norm(listing.get("hull_material") or "")
    field_says_alu = bool(mat and re.search(ALUMINIUM, mat))
    field_says_other = bool(mat and re.search(NOT_ALUMINIUM, mat))
    text_says_alu, alu_snippet = aluminium_hull_evidence(text)
    is_alu = field_says_alu or (not field_says_other and text_says_alu)
    material_known = bool(mat) or text_says_alu or bool(re.search(NOT_ALUMINIUM, text))

    if field_says_other:
        reasons_fail.append(f"hull is {parse_material(mat)}, not aluminium")
    elif not is_alu:
        reasons_fail.append("aluminium hull not established anywhere in the listing")
    elif not field_says_alu:
        warnings.append(
            "aluminium hull inferred from the text, not a spec field — confirm "
            f"with the broker. Evidence: {alu_snippet}")

    # ---- Hard filter: length --------------------------------------------
    loa_m = listing.get("loa_m")
    loa_ft = round(loa_m * FT_PER_M, 1) if loa_m else None
    tol = crit["length_ft"].get("tolerance_ft", 0)
    if loa_ft is None:
        warnings.append("LOA unknown — verify")
    elif loa_ft < crit["length_ft"]["min"] - tol:
        reasons_fail.append(f"LOA {loa_ft}ft below {crit['length_ft']['min']}ft")
    elif loa_ft > crit["length_ft"]["max"] + tol:
        reasons_fail.append(f"LOA {loa_ft}ft above {crit['length_ft']['max']}ft")

    # ---- Hard filter: age ------------------------------------------------
    year = listing.get("year")
    this_year = datetime.date.today().year
    min_year = this_year - crit["year"]["max_age_years"]
    if year is None:
        warnings.append("year unknown — verify")
    elif year < min_year:
        reasons_fail.append(f"built {year}, older than {crit['year']['max_age_years']} years")

    # ---- Hard filter: price ---------------------------------------------
    usd = listing.get("price_usd")
    if usd is None:
        warnings.append("price not published (POA) — enquire")
    elif usd < crit["price_usd"]["min"]:
        warnings.append(f"asking ${usd:,.0f} is below the ${crit['price_usd']['min']:,} floor")
    elif usd > crit["price_usd"]["max"]:
        reasons_fail.append(f"asking ${usd:,.0f} exceeds ${crit['price_usd']['max']:,}")

    # ---- Beachability ----------------------------------------------------
    beach_score, beach_ev = _scan(text, BEACHABLE)
    dmin, dmax = listing.get("draft_min_m"), listing.get("draft_max_m")
    if dmin and dmax and dmin > 0:
        ratio = dmax / dmin
        if dmin <= 1.0 and ratio >= 1.8:
            beach_score += 35
            beach_ev.append({"label": "draft geometry", "weight": 35,
                             "matched": f"{dmin}m / {dmax}m",
                             "context": f"board-up draft {dmin}m with {ratio:.1f}x range — consistent with an integral centreboarder"})
        elif dmin <= 1.4 and ratio >= 1.6:
            beach_score += 20
            beach_ev.append({"label": "draft geometry", "weight": 20,
                             "matched": f"{dmin}m / {dmax}m",
                             "context": f"board-up draft {dmin}m, {ratio:.1f}x range — probably liftable, check for a bulb"})
        elif dmin > 1.8:
            beach_score -= 15
            beach_ev.append({"label": "draft geometry", "weight": -15,
                             "matched": f"{dmin}m / {dmax}m",
                             "context": f"board-up draft still {dmin}m — too deep to sit on the bottom"})
    elif dmax and not dmin:
        warnings.append(f"only one draft figure ({dmax}m) — lifting keel unconfirmed")

    # ---- Ice / plating ---------------------------------------------------
    ice_score, ice_ev = _scan(text, ICE_AND_PLATING)
    plates = extract_plate_mm(text)
    hull_plates = [p["mm"] for p in plates if p["kind"] == "hull"]
    amb_plates = [p["mm"] for p in plates if p["kind"] == "ambiguous"]
    plate_max = max(hull_plates) if hull_plates else (max(amb_plates) if amb_plates else None)
    plate_is_confirmed_hull = bool(hull_plates)
    pc = crit["hull_plate_mm"]
    if plate_max is not None:
        if plate_max < pc["reject_below"]:
            if plate_is_confirmed_hull:
                reasons_fail.append(f"hull plating {plate_max}mm — too thin")
            else:
                ice_score -= 30
                warnings.append(
                    f"a {plate_max}mm figure appears but is not clearly the hull "
                    f"(may be doghouse/deck) — confirm hull plate thickness")
        elif plate_max < pc["weak_below"]:
            ice_score -= 25
            warnings.append(f"hull plating only {plate_max}mm — thin for ice")
        elif plate_max >= pc["excellent_at"]:
            ice_score += 30
        elif plate_max >= pc["good_at"]:
            ice_score += 20
        else:
            ice_score += 5
    else:
        warnings.append("hull plate thickness not stated — must confirm with broker")

    # ---- Insulation ------------------------------------------------------
    ins_score, ins_ev = _scan(text, INSULATION)
    if ins_score == 0:
        warnings.append("insulation not mentioned — must confirm")

    # ---- Rig -------------------------------------------------------------
    rig_score, rig_ev = _scan(text, RIG)
    rig = listing.get("rig") or _rig_from(text)
    if rig is None:
        warnings.append("rig type unknown")

    # ---- Interior --------------------------------------------------------
    int_score, int_ev = _scan(text, INTERIOR)
    cabins, heads = listing.get("cabins"), listing.get("heads")
    ic = crit["interior"]
    if cabins is not None:
        if cabins >= ic["min_cabins"]:
            int_score += 20
        else:
            int_score -= 15
            warnings.append(f"only {cabins} cabins for a crew of {ic['target_crew']}")
    else:
        warnings.append("cabin count unknown")
    if heads is not None:
        if heads >= ic["preferred_heads"]:
            int_score += 20
        elif heads >= ic["min_heads"]:
            int_score += 5
        else:
            reasons_fail.append("no heads listed")
    else:
        warnings.append("head count unknown")
    berths = listing.get("berths")
    if berths is not None and berths < ic["target_crew"]:
        warnings.append(f"{berths} berths for a target crew of {ic['target_crew']}")

    # ---- Region ----------------------------------------------------------
    country = (listing.get("country") or "").upper()
    regions = cfg.get("regions", {})
    if country and country in regions.get("remote", []):
        region_class, region_penalty = "remote", -25
        warnings.append(f"located in {country} — long delivery to eastern Canada")
    elif country and country in regions.get("easy", []):
        region_class, region_penalty = "easy", 0
    else:
        region_class, region_penalty = "unknown", -5

    # ---- Data sufficiency ------------------------------------------------
    # A record we failed to parse must never sit on the shortlist looking like
    # a clean pass just because nothing contradicted the criteria.
    known = sum(x is not None for x in (loa_m, year, usd)) + (1 if material_known else 0)
    completeness = round(known / 4.0, 2)
    if listing.get("fetch_error"):
        reasons_fail.append(f"detail page could not be fetched ({listing['fetch_error']})")
    elif known < 2:
        reasons_fail.append(
            "insufficient data extracted from the listing page — the site's parser "
            "probably needs updating")

    # ---- Totals ----------------------------------------------------------
    # Score only over dimensions the listing actually tells us something about,
    # then renormalise. Brokerage copy is wildly uneven: scoring an unmentioned
    # dimension as zero would rank a terse ad for a perfect boat below a
    # chatty ad for a mediocre one. `confidence` carries what we didn't learn.
    subscores = {
        "beachable": max(-100, min(beach_score, 100)),
        "ice_and_plating": max(-100, min(ice_score, 100)),
        "insulation": max(-100, min(ins_score, 100)),
        "rig": max(-100, min(rig_score, 100)),
        "interior": max(-100, min(int_score, 100)),
    }
    known = {
        "beachable": bool(beach_ev),
        "ice_and_plating": bool(ice_ev) or plate_max is not None,
        "insulation": bool(ins_ev),
        "rig": rig is not None,
        "interior": bool(int_ev) or cabins is not None or heads is not None,
    }
    weights = {"beachable": 0.34, "ice_and_plating": 0.20, "insulation": 0.16,
               "rig": 0.10, "interior": 0.20}

    covered = sum(w for k, w in weights.items() if known[k])
    if covered > 0:
        total = sum(subscores[k] * w for k, w in weights.items() if known[k]) / covered
    else:
        total = 0.0
    total += region_penalty
    total = max(0, min(100, round(total, 1)))
    confidence = round(covered, 2)
    # Shrink toward a neutral prior so a boat we learned almost nothing about
    # cannot outrank one we verified thoroughly. `score` stays the pure
    # evidence reading; `ranked_score` is what the shortlist sorts on.
    ranked = round(total * confidence + 30.0 * (1 - confidence), 1)

    subscores["region"] = region_penalty

    return {
        "hard_pass": not reasons_fail,
        "reasons_failed": reasons_fail,
        "data_completeness": completeness,
        "warnings": warnings,
        "score": total,
        "ranked_score": ranked,
        "confidence": confidence,
        "unknown_dimensions": sorted(k for k, v in known.items() if not v),
        "subscores": subscores,
        "rig_detected": rig,
        "hull_plate_mm_found": plates,
        "hull_plate_mm_max": plate_max,
        "hull_plate_confirmed": plate_is_confirmed_hull,
        "region_class": region_class,
        "evidence": {
            "beachable": beach_ev,
            "ice_and_plating": ice_ev,
            "insulation": ins_ev,
            "rig": rig_ev,
            "interior": int_ev,
        },
        "needs_human_check": [w for w in warnings if "confirm" in w or "verify" in w],
    }
