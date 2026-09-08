"""Snapshot persistence and change detection.

The point of this module is that the downstream Claude never has to diff
anything: it reads changes.json and gets 'new', 'price_changed', 'removed'
already computed, with per-boat price history attached.
"""
import json
import os

SCHEMA_VERSION = 1


def listing_id(rec):
    return f"{rec['source']}:{rec['source_ref']}"


def load_snapshot(state_dir):
    p = os.path.join(state_dir, "snapshot.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return {r["id"]: r for r in data.get("listings", [])}
    except (OSError, ValueError, KeyError):
        return {}


def save_snapshot(state_dir, listings, run_at):
    os.makedirs(state_dir, exist_ok=True)
    tmp = os.path.join(state_dir, "snapshot.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"schema_version": SCHEMA_VERSION, "generated_at": run_at,
                   "listings": listings}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(state_dir, "snapshot.json"))


def _price_of(rec):
    p = rec.get("price") or {}
    return p.get("amount"), p.get("currency")


def merge_and_diff(previous, current, run_at, sites_ok):
    """Fold this run's listings into the previous snapshot and describe changes.

    `sites_ok` is the set of site ids that returned a usable index this run.
    A boat is only marked 'removed' if its own site actually answered -- so a
    blocked or broken site never produces phantom removals.
    """
    changes = {"new": [], "price_changed": [], "returned": [],
               "removed": [], "updated": []}
    merged = {}
    cur_ids = set()

    for rec in current:
        lid = listing_id(rec)
        cur_ids.add(lid)
        prev = previous.get(lid)
        rec = dict(rec)
        rec["id"] = lid
        rec["last_seen"] = run_at
        amount, currency = _price_of(rec)

        if prev is None:
            rec["first_seen"] = run_at
            rec["price_history"] = ([{"at": run_at, "amount": amount, "currency": currency}]
                                    if amount is not None else [])
            rec["listing_status"] = "active"
            merged[lid] = rec
            changes["new"].append(_summary(rec))
            continue

        rec["first_seen"] = prev.get("first_seen", run_at)
        history = list(prev.get("price_history") or [])
        prev_amount, prev_currency = _price_of(prev)

        if amount is not None and (prev_amount != amount or prev_currency != currency):
            history.append({"at": run_at, "amount": amount, "currency": currency})
            delta = (amount - prev_amount) if prev_amount is not None else None
            pct = (round(delta / prev_amount * 100, 1)
                   if delta is not None and prev_amount else None)
            changes["price_changed"].append({
                **_summary(rec),
                "previous_amount": prev_amount, "previous_currency": prev_currency,
                "new_amount": amount, "new_currency": currency,
                "delta": delta, "pct_change": pct,
                "direction": ("reduced" if delta and delta < 0
                              else "increased" if delta else "changed"),
            })
        rec["price_history"] = history

        if prev.get("listing_status") == "removed":
            changes["returned"].append(_summary(rec))
        rec["listing_status"] = "active"

        for field in ("year", "loa_m", "draft_min_m", "draft_max_m",
                      "cabins", "heads", "location", "hull_material"):
            if prev.get(field) != rec.get(field) and rec.get(field) is not None \
                    and prev.get(field) is not None:
                changes["updated"].append({
                    **_summary(rec), "field": field,
                    "from": prev.get(field), "to": rec.get(field)})
        merged[lid] = rec

    # Carry forward anything we didn't see this run.
    for lid, prev in previous.items():
        if lid in cur_ids:
            continue
        rec = dict(prev)
        site = rec.get("source")
        if site in sites_ok:
            if rec.get("listing_status") != "removed":
                rec["listing_status"] = "removed"
                rec["removed_at"] = run_at
                changes["removed"].append(_summary(rec))
        else:
            rec["listing_status"] = rec.get("listing_status", "active")
            rec["stale_since"] = run_at   # site unreachable; status unknown
        merged[lid] = rec

    return list(merged.values()), changes


def _summary(rec):
    p = rec.get("price") or {}
    a = rec.get("assessment") or {}
    return {
        "id": rec.get("id"),
        "title": rec.get("title"),
        "builder": rec.get("builder"),
        "year": rec.get("year"),
        "loa_m": rec.get("loa_m"),
        "price_amount": p.get("amount"),
        "price_currency": p.get("currency"),
        "price_usd": rec.get("price_usd"),
        "landed_total_usd": (rec.get("landed_cost") or {}).get("total_usd"),
        "location": rec.get("location"),
        "url": rec.get("url"),
        "score": a.get("score"),
        "ranked_score": a.get("ranked_score"),
        "confidence": a.get("confidence"),
        "also_listed_at": rec.get("also_listed_at"),
        "hard_pass": a.get("hard_pass"),
        "source": rec.get("source"),
    }


def append_history(state_dir, run_at, changes):
    os.makedirs(state_dir, exist_ok=True)
    p = os.path.join(state_dir, "history.jsonl")
    with open(p, "a", encoding="utf-8") as fh:
        for kind, items in changes.items():
            for item in items:
                fh.write(json.dumps({"at": run_at, "event": kind, **item},
                                    ensure_ascii=False) + "\n")


def write_json(path, obj):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ------------------------------------------------------ cross-source dedup ---

_STOP = {"annee", "année", "ttc", "ht", "eur", "usd", "alu", "aluminium",
         "voilier", "plan", "de", "du", "la", "le", "les", "en", "et",
         "nouveau", "nouveaute", "nouveauté", "vendu", "m"}


def _tokens(rec):
    import re
    import unicodedata
    parts = " ".join(str(rec.get(k) or "") for k in ("builder", "model", "title"))
    parts = unicodedata.normalize("NFKD", parts.lower())
    parts = "".join(c for c in parts if not unicodedata.combining(c))
    toks = set()
    for t in re.findall(r"[a-z]+|\d+", parts):
        if t in _STOP or len(t) < 2:
            continue
        if t.isdigit() and (len(t) > 3 or int(t) > 100 or int(t) == 0):
            continue          # years, prices and the "000" of "330 000"
        toks.add(t)
    return toks


def link_duplicates(listings):
    """Flag the same hull listed by more than one broker.

    Brokers routinely co-list; without this the shortlist double-counts boats
    and a price cut at one broker looks like two separate events.
    """
    groups = []
    for rec in listings:
        if rec.get("listing_status") == "removed":
            continue
        rec.setdefault("id", listing_id(rec))   # dedup may run before merge
        toks, year, loa = _tokens(rec), rec.get("year"), rec.get("loa_m")
        placed = False
        for g in groups:
            for other in g:
                otoks = _tokens(other)
                overlap = toks & otoks
                # Two boats are only the same hull if they share a real word,
                # not merely a pair of digits from their prices.
                if len(overlap) < 2 or not any(t.isalpha() for t in overlap):
                    continue
                oy, ol = other.get("year"), other.get("loa_m")
                if year and oy and abs(year - oy) > 1:
                    continue
                if loa and ol and abs(loa - ol) > 0.4:
                    continue
                g.append(rec)
                placed = True
                break
            if placed:
                break
        if not placed:
            groups.append([rec])

    for g in groups:
        if len(g) < 2:
            continue
        for rec in g:
            rec["also_listed_at"] = [
                {"id": o["id"], "source": o.get("source"), "url": o.get("url"),
                 "price_amount": (o.get("price") or {}).get("amount"),
                 "price_currency": (o.get("price") or {}).get("currency")}
                for o in g if o["id"] != rec["id"]]
        # Keep the most completely parsed record as the group's primary.
        best = max(g, key=lambda r: ((r.get("assessment") or {}).get("confidence", 0),
                                     (r.get("assessment") or {}).get("data_completeness", 0)))
        for rec in g:
            rec["duplicate_group"] = best["id"]
            rec["is_primary_listing"] = rec["id"] == best["id"]
    return listings
