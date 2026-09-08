"""Ingest saved-search alert emails.

This is how the sites that refuse automated requests (YachtWorld, YachtFocus,
boat24) get back into the pipeline: you create a saved search in your browser,
they email you when something matches, and this adapter reads that mailbox.
It is the sanctioned route, and it is *faster* than scraping -- you hear when
a boat lists rather than up to a day later.

Reads either a local maildir/mbox (no credentials at all) or IMAP. Everything
sensitive comes from the environment, never from config.json:

    YACHTSCOUT_IMAP_HOST      imap.example.com
    YACHTSCOUT_IMAP_USER      you@example.com
    YACHTSCOUT_IMAP_PASS      an app-specific password, not your main one
    YACHTSCOUT_IMAP_FOLDER    default "INBOX"
    YACHTSCOUT_IMAP_SSL       default "1"

Or in config.json:  "maildir": "~/Maildir/.Boats/new"  |  "mbox": "~/mail/boats"

Email bodies are attacker-influenceable text. Nothing here is executed or
followed -- links are recorded, never fetched -- and every record is tagged
`untrusted_source: true` so the reading agent treats it as data.
"""
import email
import email.policy
import os
import re
import urllib.parse

import parse

# Which hosts we recognise as carrying a boat listing, and how to pull an id.
KNOWN = [
    (re.compile(r"(?i)yachtworld\.[a-z.]+/(?:yacht|boats)/.*?(\d{6,})"), "yachtworld"),
    (re.compile(r"(?i)yachtworld\.[a-z.]+/[^\s\"'<>]+"), "yachtworld"),
    (re.compile(r"(?i)yachtfocus\.com/[^\s\"'<>]+"), "yachtfocus"),
    (re.compile(r"(?i)boat24\.com/[^\s\"'<>]+"), "boat24"),
    (re.compile(r"(?i)boats\.com/[^\s\"'<>]+"), "boatsdotcom"),
    (re.compile(r"(?i)theyachtmarket\.com/[^\s\"'<>]+"), "theyachtmarket"),
    (re.compile(r"(?i)rightboat\.com/[^\s\"'<>]+"), "rightboat"),
    (re.compile(r"(?i)annoncesbateau\.com/[^\s\"'<>]+"), "annoncesbateau"),
]

URL_RE = re.compile(r"https?://[^\s\"'<>\)\]]+")


def collect(site_cfg, ctx):
    report = {"site": site_cfg["id"], "index_ok": False, "index_count": 0,
              "details_fetched": 0, "messages_read": 0, "errors": []}
    messages = []

    try:
        if site_cfg.get("maildir"):
            messages = _read_maildir(os.path.expanduser(site_cfg["maildir"]))
        elif site_cfg.get("mbox"):
            messages = _read_mbox(os.path.expanduser(site_cfg["mbox"]))
        elif os.environ.get("YACHTSCOUT_IMAP_HOST"):
            messages = _read_imap(site_cfg, ctx)
        else:
            report["errors"].append(
                "no mail source configured — set YACHTSCOUT_IMAP_HOST or add "
                "\"maildir\"/\"mbox\" to this site's config entry")
            return [], report
    except Exception as exc:
        report["errors"].append(f"{exc.__class__.__name__}: {exc}")
        return [], report

    report["messages_read"] = len(messages)
    report["index_ok"] = True

    records, seen = [], set()
    for msg in messages:
        try:
            records.extend(_from_message(msg, seen, site_cfg))
        except Exception as exc:
            report["errors"].append(f"parse failure: {exc.__class__.__name__}: {exc}")
    report["index_count"] = len(records)
    return records, report


# ------------------------------------------------------------- mail access ---

def _read_maildir(path):
    import mailbox
    if os.path.isdir(os.path.join(path, "new")):
        md = mailbox.Maildir(path, factory=None)
        return [email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
                for m in md]
    out = []
    for name in sorted(os.listdir(path))[:500]:
        fp = os.path.join(path, name)
        if os.path.isfile(fp):
            with open(fp, "rb") as fh:
                out.append(email.message_from_binary_file(fh, policy=email.policy.default))
    return out


def _read_mbox(path):
    import mailbox
    mb = mailbox.mbox(path)
    return [email.message_from_bytes(m.as_bytes(), policy=email.policy.default)
            for m in mb]


def _read_imap(site_cfg, ctx):
    import imaplib
    host = os.environ["YACHTSCOUT_IMAP_HOST"]
    user = os.environ.get("YACHTSCOUT_IMAP_USER")
    pw = os.environ.get("YACHTSCOUT_IMAP_PASS")
    folder = os.environ.get("YACHTSCOUT_IMAP_FOLDER", site_cfg.get("folder", "INBOX"))
    if not (user and pw):
        raise RuntimeError("YACHTSCOUT_IMAP_USER / _PASS not set")

    use_ssl = os.environ.get("YACHTSCOUT_IMAP_SSL", "1") != "0"
    M = imaplib.IMAP4_SSL(host) if use_ssl else imaplib.IMAP4(host)
    try:
        M.login(user, pw)
        M.select(folder, readonly=True)      # readonly: never mutate the mailbox
        days = site_cfg.get("lookback_days", 30)
        import datetime
        since = (datetime.date.today() - datetime.timedelta(days=days)).strftime("%d-%b-%Y")
        typ, data = M.search(None, f'(SINCE {since})')
        ids = (data[0].split() if data and data[0] else [])[-site_cfg.get("max_messages", 200):]
        out = []
        for i in ids:
            typ, d = M.fetch(i, "(RFC822)")
            if typ == "OK" and d and d[0]:
                out.append(email.message_from_bytes(d[0][1], policy=email.policy.default))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- parsing ---

def _blocks(msg):
    """Yield (visible_text, [urls]) per block of the message.

    Alert emails put the boat's details and its link in the same paragraph or
    table row, so pairing them blockwise keeps one boat's price from being
    attached to the next boat's link.
    """
    out = []
    parts = list(msg.walk()) if msg.is_multipart() else [msg]
    for part in parts:
        ct = part.get_content_type()
        if ct not in ("text/plain", "text/html"):
            continue
        try:
            payload = part.get_content()
        except Exception:
            continue
        if not payload:
            continue
        if ct == "text/html":
            chunks = re.split(r"(?i)</(?:p|div|tr|td|li|table|h[1-6])>|<br\s*/?>\s*<br\s*/?>",
                              payload)
            for chunk in chunks:
                urls = re.findall(r'(?is)<a[^>]+href=["\']([^"\']+)["\']', chunk)
                if urls:
                    out.append((parse.html_to_text(chunk), urls))
        else:
            for chunk in re.split(r"\n\s*\n", payload):
                urls = URL_RE.findall(chunk)
                if urls:
                    out.append((chunk, urls))
    return out


def _clean_link(url):
    """Strip tracking parameters and unwrap redirector links."""
    url = url.rstrip(".,);'\"")
    p = urllib.parse.urlsplit(url)
    for key in ("url", "u", "target", "redirect", "r"):
        qs = urllib.parse.parse_qs(p.query)
        if key in qs and qs[key] and qs[key][0].startswith("http"):
            return _clean_link(qs[key][0])
    keep = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
            if not re.match(r"(?i)^(utm_|mc_|_hs|cid|eid|trk|source|campaign)", k)]
    return urllib.parse.urlunsplit(
        (p.scheme, p.netloc, p.path, urllib.parse.urlencode(keep), ""))


# Index/search pages rather than an individual boat.
_INDEX_PATH = re.compile(
    r"(?i)/(boats-for-sale|search|results|saved-?search|unsubscribe|preferences|"
    r"account|login|help|privacy|terms)/?$|^/?$")


def _is_listing_url(url):
    path = urllib.parse.urlsplit(url).path
    if _INDEX_PATH.search(path):
        return False
    if not re.search(r"/(yacht|boat|listing|inserat|occasion|used|annonce)", path, re.I):
        return False
    # An individual listing carries an id or a slug with several words.
    tail = path.rstrip("/").rsplit("/", 1)[-1]
    return bool(re.search(r"\d{4,}", path) or tail.count("-") >= 2)


def _from_message(msg, seen, site_cfg):
    subject = parse.clean(str(msg.get("Subject", ""))) or ""
    sender = parse.clean(str(msg.get("From", ""))) or ""
    date = parse.clean(str(msg.get("Date", ""))) or ""

    out = []
    for block_text, urls in _blocks(msg):
        # Numbers inside the URL (listing ids) are not prices or years.
        ctx = re.sub(r"https?://\S+", " ", block_text)
        ctx = re.sub(r"\s+", " ", ctx).strip()
        for raw_url in urls:
            url = _clean_link(raw_url)
            site = None
            for pattern, name in KNOWN:
                if pattern.search(url):
                    site = name
                    break
            if not site or not _is_listing_url(url):
                continue
            key = re.sub(r"[?#].*$", "", url).lower()
            if key in seen:
                continue
            seen.add(key)

            out.append({
                "source": f"email:{site}",
                "source_ref": _ref(url),
                "url": url,
                "title": _title(ctx) or subject,
                "price": parse.parse_price(ctx),
                "year": parse.parse_year(ctx),
                "loa_m": parse.parse_length(ctx),
                "description": parse.clean(ctx)[:2500],
                "specs_raw": {"email_subject": subject, "email_from": sender,
                              "email_date": date},
                # Mail is attacker-influenceable; flag it for the reading agent.
                "untrusted_source": True,
                "listing_kind": "email_alert",
                "needs_manual_open": True,
            })
    return out


def _ref(url):
    m = re.search(r"(\d{5,})", url)
    if m:
        return m.group(1)
    import hashlib
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _title(ctx_txt):
    for line in re.split(r"\s{2,}|\|", ctx_txt):
        line = line.strip()
        if 8 < len(line) < 90 and re.search(r"[A-Za-z]{3}", line) \
                and not line.lower().startswith("http"):
            return line
    return None
