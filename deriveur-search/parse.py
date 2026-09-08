"""HTML -> text and unit/price normalisation. Stdlib only."""
import html
import re

FT_PER_M = 3.280839895

_TAG_RE = re.compile(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>")
_BR_RE = re.compile(r"(?i)<(br|/p|/div|/li|/tr|/h[1-6])\s*/?>")


def html_to_text(raw):
    """Flatten HTML to readable text, preserving rough line structure."""
    if not raw:
        return ""
    s = _TAG_RE.sub(" ", raw)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = _BR_RE.sub("\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ").replace(" ", " ")
    lines = []
    for line in s.split("\n"):
        line = re.sub(r"[ \t]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def clean(s):
    if s is None:
        return None
    s = html.unescape(str(s)).replace("\xa0", " ").replace(" ", " ")
    return re.sub(r"\s+", " ", s).strip() or None


# ---------------------------------------------------------------- numbers ---

def _to_float(s):
    """Parse a number that may use ',' or '.' as decimal separator."""
    if s is None:
        return None
    s = str(s).strip().replace(" ", "").replace(" ", "").replace("'", "")
    if not s:
        return None
    if "," in s and "." in s:
        # whichever comes last is the decimal separator
        s = s.replace(",", "") if s.rindex(".") > s.rindex(",") else s.replace(".", "").replace(",", ".")
    elif "," in s:
        frac = s.rsplit(",", 1)[1]
        s = s.replace(",", ".") if len(frac) in (1, 2) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_length(text):
    """Return length in metres from a string like '14.37m', '47 ft', '14,37 m'."""
    if not text:
        return None
    t = str(text).lower()
    m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(m\b|mtr|meter|metre|m\.)", t)
    if m:
        v = _to_float(m.group(1))
        if v and 3 <= v <= 120:
            return round(v, 2)
    m = re.search(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(ft|feet|foot|')", t)
    if m:
        v = _to_float(m.group(1))
        if v and 10 <= v <= 400:
            return round(v / FT_PER_M, 2)
    m = re.search(r"^\s*(\d{1,3}(?:[.,]\d{1,2})?)\s*$", t)
    if m:
        v = _to_float(m.group(1))
        if v and 3 <= v <= 120:
            return round(v, 2)
    return None


def parse_draft(text):
    """Return (min_m, max_m). Handles '1.05/2.48m', '1,05 - 2,48 m', '2.5m'."""
    if not text:
        return (None, None)
    t = str(text).lower().replace("–", "-").replace("—", "-")
    m = re.search(r"(\d{1,2}[.,]?\d{0,2})\s*(?:m)?\s*[/\-àa]{1,3}\s*(\d{1,2}[.,]?\d{0,2})\s*m?", t)
    if m:
        a, b = _to_float(m.group(1)), _to_float(m.group(2))
        if a and b and 0.2 <= a <= 8 and 0.2 <= b <= 8:
            return (round(min(a, b), 2), round(max(a, b), 2))
    single = parse_length(t)
    if single and 0.2 <= single <= 8:
        return (None, round(single, 2))
    return (None, None)


def parse_year(text):
    if not text:
        return None
    m = re.search(r"\b(19[5-9]\d|20[0-4]\d)\b", str(text))
    return int(m.group(1)) if m else None


def parse_int(text, lo=0, hi=999):
    if text is None:
        return None
    m = re.search(r"\d+", str(text))
    if not m:
        return None
    v = int(m.group(0))
    return v if lo <= v <= hi else None


# --------------------------------------------------------------- currency ---

_CURRENCY_TOKENS = [
    (r"€|eur\b|euros?\b", "EUR"),
    (r"£|gbp\b|pounds?\b|sterling", "GBP"),
    (r"\bchf\b|\bsfr\b", "CHF"),
    (r"\bcad\b|c\$", "CAD"),
    (r"\baud\b|a\$", "AUD"),
    (r"\bnzd\b", "NZD"),
    (r"\bsek\b|\bkr\b", "SEK"),
    (r"\bnok\b", "NOK"),
    (r"\bdkk\b", "DKK"),
    (r"\bxpf\b|\bcfp\b", "XPF"),
    (r"\$|\busd\b|us\$", "USD"),
]


def parse_price(text, default_currency=None):
    """Return dict(amount, currency, tax_status, raw) or None."""
    if not text:
        return None
    t = str(text)
    low = t.lower()

    if re.search(r"(?i)\b(poa|p\.o\.a|price on application|nous consulter|"
                 r"sur demande|prix sur demande|op aanvraag|auf anfrage)\b", low):
        return {"amount": None, "currency": None, "tax_status": None, "raw": clean(t), "on_application": True}

    currency = default_currency
    for pat, code in _CURRENCY_TOKENS:
        if re.search(pat, low):
            currency = code
            break

    tax = None
    if re.search(r"\bttc\b|\bincl?\.?\s*(vat|btw)\b|vat paid|tva pay", low):
        tax = "tax_paid"
    elif re.search(r"\bht\b|\bhors taxe|\bexcl?\.?\s*(vat|btw)\b|vat not paid|ex\.? vat", low):
        tax = "tax_unpaid"

    best = None
    for m in re.finditer(r"\d[\d\s.,' ]{2,}\d|\b\d{4,}\b", t):
        v = _to_float(m.group(0))
        if v is None:
            continue
        if 5000 <= v <= 50_000_000 and (best is None or v > best):
            best = v
    if best is None:
        return None
    return {"amount": round(best, 2), "currency": currency,
            "tax_status": tax, "raw": clean(t), "on_application": False}


def to_usd(price, fx):
    if not price or price.get("amount") is None:
        return None
    cur = price.get("currency") or "EUR"
    rate = fx.get(cur)
    if rate is None:
        return None
    return round(price["amount"] * rate, 2)


def landed_cost_usd(usd, cfg):
    """Purchase price -> estimated cost landed in eastern Canada."""
    if usd is None:
        return None
    imp = cfg.get("import_to_eastern_canada", {})
    duty = usd * imp.get("duty_rate", 0.095)
    taxable = usd + duty
    tax = taxable * imp.get("provincial_and_federal_tax_rate", 0.15)
    return {
        "purchase_usd": round(usd, 2),
        "duty_usd": round(duty, 2),
        "tax_usd": round(tax, 2),
        "total_usd": round(usd + duty + tax, 2),
    }


# ------------------------------------------------------------ spec tables ---

def extract_label_value_pairs(raw_html):
    """Pull <table>/<dl>/<li> label:value pairs out of a detail page."""
    pairs = {}
    for m in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", raw_html):
        cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", m.group(1))
        if len(cells) >= 2:
            k, v = clean(html_to_text(cells[0])), clean(html_to_text(cells[1]))
            if k and v and len(k) < 60:
                pairs.setdefault(k.rstrip(":").lower(), v)
    for m in re.finditer(r"(?is)<dt[^>]*>(.*?)</dt>\s*<dd[^>]*>(.*?)</dd>", raw_html):
        k, v = clean(html_to_text(m.group(1))), clean(html_to_text(m.group(2)))
        if k and v and len(k) < 60:
            pairs.setdefault(k.rstrip(":").lower(), v)
    for m in re.finditer(r"(?is)<li[^>]*>(.*?)</li>", raw_html):
        txt = clean(html_to_text(m.group(1))) or ""
        if 3 < len(txt) < 120 and ":" in txt:
            k, _, v = txt.partition(":")
            k, v = clean(k), clean(v)
            if k and v and len(k) < 60:
                pairs.setdefault(k.lower(), v)
    return pairs


def pairs_from_lines(text, labels):
    """For sites that render specs as 'Label\\nValue' line pairs."""
    out = {}
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    lowered = [l.lower().rstrip(":") for l in lines]
    for i, lab in enumerate(lowered):
        if lab in labels and i + 1 < len(lines):
            out.setdefault(lab, lines[i + 1])
    return out


def absolute_url(base, href):
    """Join and percent-encode. Broker sites routinely emit hrefs containing
    raw spaces and accented characters, which curl rejects outright."""
    import urllib.parse
    if not href:
        return None
    url = urllib.parse.urljoin(base, html.unescape(href.strip()))
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        urllib.parse.quote(parts.path, safe="/%:@&=+$,~()!*'"),
        urllib.parse.quote(parts.query, safe="/%:@&=+$,~?"),
        "",
    ))
