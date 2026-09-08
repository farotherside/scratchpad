"""HTTP fetching via curl. No third-party deps.

Deliberately polite: identifies itself, rate-limits, caches aggressively so
repeat runs cost the target site almost nothing, and honours robots.txt.
"""
import hashlib
import json
import os
import random
import subprocess
import time
import urllib.parse
import urllib.robotparser
from datetime import datetime, timezone

_last_request_at = {}
_robots_cache = {}


class FetchResult:
    def __init__(self, url, status, body, from_cache=False, error=None):
        self.url = url
        self.status = status
        self.body = body or ""
        self.from_cache = from_cache
        self.error = error

    @property
    def ok(self):
        return self.status == 200 and len(self.body) > 500

    def __repr__(self):
        tag = "cache" if self.from_cache else "net"
        return f"<Fetch {self.status} {tag} {len(self.body)}B {self.url[:60]}>"


def _cache_path(cache_dir, url):
    h = hashlib.sha256(url.encode()).hexdigest()[:20]
    return os.path.join(cache_dir, h + ".html")


def _read_cache(cache_dir, url, ttl_hours):
    p = _cache_path(cache_dir, url)
    if not os.path.exists(p):
        return None
    age_h = (time.time() - os.path.getmtime(p)) / 3600.0
    if age_h > ttl_hours:
        return None
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return None


def _write_cache(cache_dir, url, body):
    os.makedirs(cache_dir, exist_ok=True)
    try:
        with open(_cache_path(cache_dir, url), "w", encoding="utf-8") as fh:
            fh.write(body)
    except OSError:
        pass


def robots_allows(url, user_agent):
    """Check robots.txt. Fail OPEN only if robots.txt itself is unreachable."""
    parts = urllib.parse.urlparse(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            out = subprocess.run(
                ["curl", "-sS", "-A", user_agent, "--max-time", "20", "-L",
                 origin + "/robots.txt"],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            text = out.stdout or ""
            if "<html" in text[:400].lower() or not text.strip():
                rp = None          # no usable robots.txt -> no restrictions known
            else:
                rp.parse(text.splitlines())
        except Exception:
            rp = None
        _robots_cache[origin] = rp
    rp = _robots_cache[origin]
    if rp is None:
        return True
    try:
        return rp.can_fetch(user_agent, url)
    except Exception:
        return True


def _throttle(url, cfg):
    parts = urllib.parse.urlparse(url)
    host = parts.netloc
    delay = cfg.get("delay_seconds", 4.0) + random.uniform(0, cfg.get("jitter_seconds", 2.0))
    last = _last_request_at.get(host)
    if last is not None:
        wait = delay - (time.time() - last)
        if wait > 0:
            time.sleep(wait)
    _last_request_at[host] = time.time()


def fetch(url, cfg, cache_dir, ttl_hours, force=False):
    """Fetch a URL, using cache when fresh. Returns FetchResult."""
    ua = cfg.get("user_agent", "YachtScout/1.0")

    if not force:
        cached = _read_cache(cache_dir, url, ttl_hours)
        if cached is not None:
            return FetchResult(url, 200, cached, from_cache=True)

    if not robots_allows(url, ua):
        return FetchResult(url, 999, "", error="disallowed by robots.txt")

    attempts = cfg.get("max_retries", 2) + 1
    last_err = None
    for attempt in range(attempts):
        _throttle(url, cfg)
        cmd = [
            "curl", "-sS", "-L", "--compressed",
            "-A", ua,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en,fr;q=0.8,nl;q=0.6",
            "--max-time", str(cfg.get("timeout_seconds", 45)),
            "-w", "\n___HTTP_STATUS___%{http_code}",
            url,
        ]
        try:
            # errors="replace": broker pages are served in a zoo of encodings
            # and a single stray byte must not abort the whole run.
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 errors="replace",
                                 timeout=cfg.get("timeout_seconds", 45) + 15)
        except subprocess.TimeoutExpired:
            last_err = "curl timeout"
            continue

        raw = out.stdout or ""
        status = 0
        if "___HTTP_STATUS___" in raw:
            raw, _, tail = raw.rpartition("\n___HTTP_STATUS___")
            try:
                status = int(tail.strip())
            except ValueError:
                status = 0
        if status == 200:
            _write_cache(cache_dir, url, raw)
            return FetchResult(url, 200, raw)
        last_err = f"HTTP {status}" + (f"; {out.stderr.strip()[:120]}" if out.stderr else "")
        if status in (403, 401, 404, 410):
            return FetchResult(url, status, raw, error=last_err)   # don't retry hard blocks
        time.sleep(2 + attempt * 3)

    return FetchResult(url, 0, "", error=last_err or "unknown fetch failure")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
