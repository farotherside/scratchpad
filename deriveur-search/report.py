"""Human- and Claude-readable run outputs."""
import html
import os


def _money(a, c):
    if a is None:
        return "POA"
    sym = {"EUR": "€", "GBP": "£", "USD": "$", "CAD": "C$"}.get(c, (c or "") + " ")
    return f"{sym}{a:,.0f}"


def _line(rec):
    a = rec.get("assessment") or {}
    p = rec.get("price") or {}
    bits = [f"**{rec.get('title') or rec.get('id')}**"]
    if rec.get("year"):
        bits.append(str(rec["year"]))
    if rec.get("loa_ft"):
        bits.append(f"{rec['loa_ft']}ft")
    if rec.get("draft_min_m") and rec.get("draft_max_m"):
        bits.append(f"draft {rec['draft_min_m']}–{rec['draft_max_m']}m")
    bits.append(_money(p.get("amount"), p.get("currency")))
    if rec.get("landed_cost"):
        bits.append(f"landed ≈${rec['landed_cost']['total_usd']:,.0f}")
    if rec.get("location"):
        bits.append(rec["location"])
    bits.append(f"score {a.get('score', 0)} (conf {a.get('confidence', 0)})")
    return " · ".join(bits)


def write_brief(path, run_at, shortlist, active, changes, run_report, cfg):
    L = []
    A = L.append
    A(f"# Sailboat search brief — {run_at[:10]}\n")
    c = run_report["counts"]
    A(f"{c['active']} listings tracked · **{c['shortlist']} pass the hard filters** · "
      f"{c['new']} new · {c['price_changed']} price changes · {c['removed']} gone\n")

    if run_report["sites_unreachable"]:
        A(f"> ⚠ Sites unreachable this run: **{', '.join(run_report['sites_unreachable'])}**. "
          f"Their listings are held as-is, not marked removed.\n")

    if changes["new"]:
        A("\n## 🆕 New since last run\n")
        for s in changes["new"]:
            A(f"- [{s['title']}]({s['url']}) — {_money(s['price_amount'], s['price_currency'])}"
              f" · score {s.get('score')} · {'PASSES' if s.get('hard_pass') else 'filtered out'}")

    if changes["price_changed"]:
        A("\n## 💷 Price changes\n")
        for s in changes["price_changed"]:
            arrow = "▼" if s["direction"] == "reduced" else "▲"
            pct = f" ({s['pct_change']:+}%)" if s.get("pct_change") is not None else ""
            A(f"- {arrow} [{s['title']}]({s['url']}): "
              f"{_money(s['previous_amount'], s['previous_currency'])} → "
              f"{_money(s['new_amount'], s['new_currency'])}{pct}")

    if changes["removed"]:
        A("\n## ⛔ No longer listed\n")
        for s in changes["removed"]:
            A(f"- {s['title']} ({s['source']}) — {_money(s['price_amount'], s['price_currency'])}")

    if changes["returned"]:
        A("\n## ↩️ Back on the market\n")
        for s in changes["returned"]:
            A(f"- [{s['title']}]({s['url']})")

    A("\n## ⭐ Current shortlist (passes hard filters, ranked)\n")
    if not shortlist:
        A("_Nothing passes every hard filter right now._")
    for rec in shortlist[:40]:
        a = rec.get("assessment") or {}
        A(f"\n### {_line(rec)}")
        A(f"{rec.get('url')}")
        sub = a.get("subscores", {})
        A(f"- scores — beachable {sub.get('beachable')}, ice/plating "
          f"{sub.get('ice_and_plating')}, insulation {sub.get('insulation')}, "
          f"rig {sub.get('rig')}, interior {sub.get('interior')}")
        for key in ("beachable", "insulation", "ice_and_plating"):
            for ev in (a.get("evidence", {}).get(key) or [])[:2]:
                A(f"  - _{key}_ {ev['weight']:+d} — {ev['label']}: “{ev['matched']}”")
        if rec.get("also_listed_at"):
            A("- 🔁 also listed at: " + ", ".join(
                f"[{d['source']}]({d['url']})" for d in rec["also_listed_at"]))
        if a.get("needs_human_check"):
            A(f"- ❓ **confirm with broker:** {'; '.join(a['needs_human_check'])}")

    near = sorted([r for r in active
                   if not (r.get("assessment") or {}).get("hard_pass")
                   and (r.get("assessment") or {}).get("score", 0) >= 45],
                  key=lambda r: r["assessment"]["ranked_score"], reverse=True)
    if near:
        A("\n## 🤔 Near misses (failed a hard filter but scored well)\n")
        for rec in near[:15]:
            a = rec["assessment"]
            A(f"- [{rec.get('title')}]({rec.get('url')}) — score {a['score']}; "
              f"failed: {'; '.join(a['reasons_failed'])}")

    A("\n---\n")
    A("_Scores are heuristic. `beachable` is inferred from wording and draft geometry; "
      "always confirm a lifting keel has no ballast bulb, and confirm hull plate "
      "thickness and insulation with the broker before travelling to view._")

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def write_index_html(path, run_at, run_report):
    c = run_report["counts"]
    rows = "".join(
        f"<tr><td>{html.escape(s['site'])}</td>"
        f"<td>{'ok' if s.get('index_ok') else 'unreachable'}</td>"
        f"<td>{s.get('records', 0)}</td>"
        f"<td>{html.escape('; '.join(s.get('errors') or [])[:160])}</td></tr>"
        for s in run_report["sites"])
    doc = f"""<!doctype html><meta charset="utf-8">
<title>YachtScout</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:52rem;margin:3rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}}
code{{background:#f4f4f5;padding:.1rem .3rem;border-radius:3px}}</style>
<h1>YachtScout</h1>
<p>Last run <strong>{html.escape(run_at)}</strong> — {c['active']} active,
{c['shortlist']} shortlisted, {c['new']} new, {c['price_changed']} price changes.</p>
<ul>
<li><a href="brief.md">brief.md</a> — readable summary</li>
<li><a href="shortlist.json">shortlist.json</a> — boats passing every hard filter</li>
<li><a href="listings.json">listings.json</a> — full database</li>
<li><a href="changes.json">changes.json</a> — this run's diff</li>
<li><a href="run_report.json">run_report.json</a> — scraper health</li>
</ul>
<h2>Sources</h2><table><tr><th>site</th><th>status</th><th>records</th><th>notes</th></tr>{rows}</table>
"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(doc)
