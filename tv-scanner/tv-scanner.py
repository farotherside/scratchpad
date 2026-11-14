#!/usr/bin/env python3
"""
tv-scanner.py — Compare a local TV library against TVmaze and/or TheTVDB episode data.

Usage:
    python tv-scanner.py /path/to/tv/root [options]

Options:
    --output {text,json,csv,html}  Output format (default: text)
    --missing-only                 Only show shows/episodes that have gaps
    --no-ended                     Skip shows whose status is Ended / To Be Determined
    --workers N                    Parallel API workers (default: 4)
    --no-color                     Disable color output
    --source LIST                  Databases to query, comma-separated (default: tvmaze)
    --thetvdb-apikey PATH          Path to file containing TheTVDB API key
    --tmdb-apikey PATH             Path to file containing TMDB API key / read token
    --prefer-animation             Favour animated series (for cartoon libraries)

Sources:
  tvmaze     — TVmaze (no API key required)
  episodate  — Episodate (no API key required)
  thetvdb    — TheTVDB (requires --thetvdb-apikey)
  tmdb       — The Movie Database (requires --tmdb-apikey)
  both       — tvmaze,thetvdb
  all        — every source that is usable with the keys supplied

When several sources are given, each show uses whichever database's episode
list best matches the local files.

Directory structure expected:
    <root>/
        Show Name/
            Season 1/   (or S01, Season01, s1 …)
                episode.mkv
            Season 2/
                …
"""

import argparse
import csv
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TVMAZE_BASE = "https://api.tvmaze.com"
THETVDB_BASE = "https://api4.thetvdb.com/v4"
TMDB_BASE = "https://api.themoviedb.org/3"
EPISODATE_BASE = "https://www.episodate.com/api"
REQUEST_TIMEOUT = 15          # seconds per HTTP request
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0           # seconds
RATE_LIMIT_DELAY = 0.2        # polite delay between requests (TVmaze is free/public)
THETVDB_RATE_LIMIT_DELAY = 0.5
TMDB_RATE_LIMIT_DELAY = 0.05
EPISODATE_RATE_LIMIT_DELAY = 0.3
PREFER_ANIMATION: bool = False   # set by --prefer-animation

# Show identification
MAX_CANDIDATES = 5            # search results considered per source
TITLE_HIT = 0.8               # similarity at/above which two episode titles "match"
TITLE_FLOOR = 0.6             # similarity below this counts as no match at all
LOW_CONFIDENCE = 0.5          # matches scoring below this are flagged
AMBIGUOUS_MARGIN = 0.05       # runner-up within this margin → flagged ambiguous

# File extensions considered video files
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".divx",
}

# ---------------------------------------------------------------------------
# Color support
# ---------------------------------------------------------------------------

def _detect_color_support() -> bool:
    if not sys.stdout.isatty():
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm:
        return True
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program:
        return True
    term = os.environ.get("TERM", "").lower()
    no_color_terms = {"", "dumb", "unknown"}
    return term not in no_color_terms


USE_COLOR: bool = False


class _C:
    RESET         = "\033[0m"
    BOLD          = "\033[1m"
    DIM           = "\033[2m"
    RED           = "\033[31m"
    GREEN         = "\033[32m"
    YELLOW        = "\033[33m"
    BLUE          = "\033[34m"
    CYAN          = "\033[36m"
    BRIGHT_RED    = "\033[91m"
    BRIGHT_GREEN  = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_CYAN   = "\033[96m"
    BRIGHT_WHITE  = "\033[97m"


def _col(code: str, text: str) -> str:
    if not USE_COLOR:
        return text
    return f"{code}{text}{_C.RESET}"

def _bold(text: str) -> str: return _col(_C.BOLD, text)
def _red(text: str) -> str:  return _col(_C.BRIGHT_RED, text)
def _green(text: str) -> str: return _col(_C.BRIGHT_GREEN, text)
def _yellow(text: str) -> str: return _col(_C.BRIGHT_YELLOW, text)
def _cyan(text: str) -> str:  return _col(_C.BRIGHT_CYAN, text)
def _dim(text: str) -> str:   return _col(_C.DIM, text)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class LocalEpisode:
    season: int
    episode: int
    path: Path
    title: str = ""        # episode title parsed from the filename, if any


@dataclass
class RemoteEpisode:
    season: int
    episode: int
    name: str
    airdate: str           # may be empty string


@dataclass
class ShowReport:
    show_name: str
    matched_title: Optional[str]
    tvmaze_id: Optional[int]
    status: Optional[str]
    local_seasons: set
    local_episodes: list
    remote_episodes: list
    missing: list
    extra: list
    lookup_error: Optional[str]
    source_used: str = "tvmaze"   # key of SOURCES, or "none" when nothing matched
    source_id: Optional[int] = None            # series ID on the source that was used
    source_url: str = ""                       # that series' page on the source
    match_confidence: Optional[float] = None   # 0..1 score of the chosen show
    match_note: str = ""                       # human-readable evidence for the match
    match_flag: str = ""                       # "", "low" or "ambiguous"
    alternatives: list = field(default_factory=list)   # other candidates considered

    @property
    def ok(self) -> bool:
        return not self.missing and not self.lookup_error


# ---------------------------------------------------------------------------
# Generic HTTP helper
# ---------------------------------------------------------------------------
# Disk cache for API responses (set up in main(); disabled when CACHE_DIR is None)
CACHE_DIR: Optional[Path] = None
CACHE_TTL: float = 24 * 3600      # seconds a cached response stays fresh
CACHE_REFRESH: bool = False       # ignore existing entries but still write new ones
_cache_stats = {"hit": 0, "miss": 0}


def _cache_path(url: str, params: Optional[Dict[str, Any]]) -> Optional[Path]:
    if CACHE_DIR is None:
        return None
    key = url + "?" + "&".join(
        f"{k}={v}" for k, v in sorted((params or {}).items()) if k != "api_key"
    )
    return CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".json")


def _cache_read(path: Optional[Path]) -> Any:
    if path is None or CACHE_REFRESH:
        return None
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            return None
        with path.open(encoding="utf-8") as fh:
            return json.load(fh)["body"]
    except (OSError, ValueError, KeyError):
        return None


def _cache_write(path: Optional[Path], url: str, body: Any) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"url": url, "body": body}, fh)
        os.replace(tmp, path)             # atomic: safe with parallel workers
    except OSError:
        pass                              # caching is best-effort


def _request(url: str, params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None, rate_delay: float = RATE_LIMIT_DELAY) -> Union[Dict[str, Any], List[Any]]:
    """GET with disk cache, retry and polite rate-limit."""
    cpath = _cache_path(url, params)
    cached = _cache_read(cpath)
    if cached is not None:
        _cache_stats["hit"] += 1
        return cached
    _cache_stats["miss"] += 1

    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", RETRY_BACKOFF * (attempt + 1)))
                time.sleep(retry_after)
                continue
            if 400 <= resp.status_code < 500:
                resp.raise_for_status()   # client errors won't improve on retry
            resp.raise_for_status()
            time.sleep(rate_delay)
            body = resp.json()
            _cache_write(cpath, url, body)
            return body
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if 400 <= status < 500 or attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BACKOFF * (attempt + 1))
        except requests.RequestException:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_BACKOFF * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}")


# ---------------------------------------------------------------------------
# Season-folder parsing
# ---------------------------------------------------------------------------
_SEASON_RE = re.compile(
    r"""
    (?:
        season[\s._-]*(\d+)
      | s[\s._-]?(\d+)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_season_number(folder_name: str) -> Optional[int]:
    m = _SEASON_RE.search(folder_name)
    if m:
        val = m.group(1) or m.group(2)
        return int(val)
    bare = re.fullmatch(r"\d+", folder_name.strip())
    if bare:
        return int(bare.group())
    return None


# ---------------------------------------------------------------------------
# Episode filename parsing
# ---------------------------------------------------------------------------

# Multi-episode: S01E01-E03 or S01E01E02 (returns season + range of episodes)
_MULTI_EP_RE = re.compile(
    r"[Ss](\d+)[Ee](\d+)[-–]?[Ee](\d+)",
    re.IGNORECASE,
)

# Single-episode patterns tried in order
_EP_PATTERNS = [
    re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE),
    re.compile(r"(\d+)[xX](\d+)"),
    re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)"),
]


def _parse_episode_token(stem: str) -> Tuple[List[Tuple[int, int]], int]:
    """Return ([(season, episode), …], index just past the episode token)."""
    # Try multi-episode first: S01E01-E03 or S01E01E02
    m = _MULTI_EP_RE.search(stem)
    if m:
        s, e_start, e_end = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 0 < s < 100 and 0 < e_start < 300 and 0 < e_end < 300 and e_end >= e_start:
            return [(s, e) for e in range(e_start, e_end + 1)], m.end()

    # Single-episode patterns
    for pat in _EP_PATTERNS:
        m = pat.search(stem)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            if 0 < s < 100 and 0 < e < 300:
                return [(s, e)], m.end()

    return [], 0


def parse_episodes_from_filename(filename: str) -> List[Tuple[int, int]]:
    """Return list of (season, episode) tuples covered by this file.

    Handles:
      S01E01           → [(1, 1)]
      S01E01-E03       → [(1, 1), (1, 2), (1, 3)]
      S01E01E02        → [(1, 1), (1, 2)]
      1x02             → [(1, 2)]
    """
    return _parse_episode_token(Path(filename).stem)[0]


def parse_episode_from_filename(filename: str) -> Optional[Tuple[int, int]]:
    """Legacy single-result wrapper — returns first (season, episode) or None."""
    results = parse_episodes_from_filename(filename)
    return results[0] if results else None


# Release-name noise that marks the end of an episode title. Deliberately limited to
# unambiguous tokens — words like "web", "proper" or "extended" occur in real titles.
_RELEASE_JUNK_RE = re.compile(
    r"""(?:^|[\s._\-\[(])(?:
        \d{3,4}[pi] | hdr10 | 10bit
      | x\.?26[45] | h\.?26[45] | hevc | xvid | divx
      | web[\s._-]?(?:dl|rip) | blu[\s._-]?ray | b[dr]rip | hdtv | pdtv | dvdrip | remux
      | repack | amzn | dsnp | hmax | atvp
      | aac(?:\d\.\d)? | e?ac3 | ddp?\d\.\d
    )(?=$|[\s._\-\])]).*$""",
    re.IGNORECASE | re.VERBOSE,
)

# Titles that carry no identifying information ("Episode 4", "Part 2", "Pilot" …)
_GENERIC_TITLE_RE = re.compile(
    r"^(?:(?:episode|ep|chapter|part|pt|folge|episodio|épisode|aflevering|week|day|show)\s*#?\s*"
    r"(?:\d+|[ivxlc]+|one|two|three|four|five|six|seven|eight|nine|ten)|pilot|tba|tbd|\d+)$",
    re.IGNORECASE,
)


def parse_title_from_filename(filename: str) -> str:
    """Extract the episode title that follows the SxxEyy token, if any.

      "S01E03 - ...and the Bag's in the River.mkv"   → "...and the Bag's in the River"
      "Show.Name.S02E04.Some.Title.1080p.WEB-DL.mkv" → "Some Title"
      "Show - 1x02.mkv"                              → ""
    """
    stem = Path(filename).stem
    eps, end = _parse_episode_token(stem)
    if not eps:
        return ""
    rest = stem[end:]
    # Dot/underscore-separated release names: convert separators to spaces
    if " " not in rest.strip():
        rest = re.sub(r"[._]+", " ", rest)
    rest = re.sub(r"\[[^\]]*\]", " ", rest)          # [group] / [tags]
    rest = _RELEASE_JUNK_RE.sub("", rest)
    rest = rest.strip(" \t-–—_:")
    rest = re.sub(r"\s+", " ", rest)
    return rest


def _norm(text: str) -> str:
    """Normalise a title for fuzzy comparison (case, accents, punctuation)."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    text = text.replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.strip()


def _is_generic_title(title: str) -> bool:
    n = _norm(title)
    return not n or bool(_GENERIC_TITLE_RE.match(n))


_PART_SUFFIX_RE = re.compile(r"\s+(?:part\s+)?(?:\d|[ivx]+)$")


def _title_similarity(a: str, b: str) -> float:
    """0..1 similarity of two already-normalised titles."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    best = difflib.SequenceMatcher(None, a, b).ratio()
    # "Six of One (2)" vs "Six of One" / "… Part 2"
    a2, b2 = _PART_SUFFIX_RE.sub("", a), _PART_SUFFIX_RE.sub("", b)
    if (a2 != a or b2 != b) and a2 and a2 == b2:
        best = max(best, 0.95)
    return best


# Explicit show IDs live in an overrides file kept OUTSIDE the media library —
# the library's file and folder names are never used to carry tool metadata.
_ID_SPEC_RE = re.compile(
    r"^\s*(tvdb|thetvdb|tvmaze|tmdb|episodate)(?:id)?\s*[-=:\s]\s*(\d+)\s*$", re.IGNORECASE
)

# folder name → (source, id), loaded from the overrides file in main()
OVERRIDES: Dict[str, Tuple[str, int]] = {}
DEFAULT_OVERRIDES_PATH = Path.home() / ".config" / "tv-scanner" / "overrides.json"


def _id_from_match(m) -> Tuple[str, int]:
    name = m.group(1).lower()
    return ("thetvdb" if name == "tvdb" else name, int(m.group(2)))


def load_overrides(path: Path) -> Dict[str, Tuple[str, int]]:
    """Read {"Folder Name": "tvmaze:526" | "tvdb:…" | "tmdb:…" | "episodate:…"} from JSON."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object mapping folder names to IDs")
    out: Dict[str, Tuple[str, int]] = {}
    for folder, spec in data.items():
        if folder.startswith("_"):
            continue                      # allow "_comment" keys
        m = _ID_SPEC_RE.match(str(spec))
        if not m:
            raise ValueError(f"bad ID for {folder!r}: {spec!r} (use \"tvmaze:123\", \"tvdb:123\", \"tmdb:123\" or \"episodate:123\")")
        out[folder] = _id_from_match(m)
    return out


_YEAR_SUFFIX_RE = re.compile(r"\s*[\(\[]?\b((?:19|20)\d{2})\b[\)\]]?\s*$")


def split_show_year(folder_name: str) -> Tuple[str, Optional[int]]:
    """"Doctor Who (2005)" → ("Doctor Who", 2005).  A bare year ("1923") is a name."""
    m = _YEAR_SUFFIX_RE.search(folder_name)
    if m and folder_name[:m.start()].strip():
        return folder_name[:m.start()].strip(), int(m.group(1))
    return folder_name.strip(), None


# ---------------------------------------------------------------------------
# Local library scan
# ---------------------------------------------------------------------------
def scan_local_library(root: Path) -> Dict[str, List[LocalEpisode]]:
    library: Dict[str, List[LocalEpisode]] = {}

    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    for show_dir in sorted(root.iterdir()):
        if not show_dir.is_dir() or show_dir.name.startswith("."):
            continue

        if show_dir.name.startswith("._"):
            continue

        episodes = []

        for season_dir in sorted(show_dir.iterdir()):
            if not season_dir.is_dir():
                continue
            season_num = parse_season_number(season_dir.name)
            if season_num is None:
                continue

            for f in sorted(season_dir.iterdir()):
                if f.name.startswith("._"):
                    continue
                if f.suffix.lower() not in VIDEO_EXTS:
                    continue
                parsed = parse_episodes_from_filename(f.name)
                if parsed:
                    title = parse_title_from_filename(f.name)
                    # Emit one LocalEpisode per episode number covered by the file
                    for ep_s, ep_e in parsed:
                        episodes.append(LocalEpisode(season=ep_s, episode=ep_e, path=f, title=title))
                else:
                    episodes.append(LocalEpisode(season=season_num, episode=0, path=f))

        if episodes:
            library[show_dir.name] = episodes

    return library


# ---------------------------------------------------------------------------
# TVmaze lookups
# ---------------------------------------------------------------------------
_tvmaze_episode_cache: dict = {}


def tvmaze_fetch_episodes(tvmaze_id: int) -> list:
    if tvmaze_id in _tvmaze_episode_cache:
        return _tvmaze_episode_cache[tvmaze_id]

    data = _request(f"{TVMAZE_BASE}/shows/{tvmaze_id}/episodes")
    episodes = []
    for ep in data:
        if ep.get("type", "regular") != "regular":
            continue
        s = ep.get("season")
        e = ep.get("number")
        if s and e:
            episodes.append(RemoteEpisode(
                season=s,
                episode=e,
                name=ep.get("name", ""),
                airdate=ep.get("airdate", ""),
            ))

    _tvmaze_episode_cache[tvmaze_id] = episodes
    return episodes


def tvmaze_search_shows(show_name: str) -> List[dict]:
    """Return TVmaze search results (best first), excluding unaired shows."""
    results = _request(f"{TVMAZE_BASE}/search/shows", params={"q": show_name})
    return [
        r["show"] for r in (results or [])
        if r["show"].get("status", "") != "In Development"
    ]


# ---------------------------------------------------------------------------
# TheTVDB lookups
# ---------------------------------------------------------------------------
_thetvdb_token: Optional[str] = None
_thetvdb_episode_cache: dict = {}


def thetvdb_init(api_key: str) -> None:
    """Authenticate with TheTVDB and store bearer token."""
    global _thetvdb_token
    resp = requests.post(
        f"{THETVDB_BASE}/login",
        json={"apikey": api_key},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    _thetvdb_token = resp.json()["data"]["token"]


def _thetvdb_headers() -> dict:
    if not _thetvdb_token:
        raise RuntimeError("TheTVDB token not initialized. Call thetvdb_init() first.")
    return {"Authorization": f"Bearer {_thetvdb_token}"}


def thetvdb_search_shows(show_name: str, year: Optional[int] = None) -> List[dict]:
    """Return TheTVDB series search results (best first), optionally limited to a year."""
    params: Dict[str, Any] = {"query": show_name, "type": "series"}
    if year:
        params["year"] = year
    try:
        data = _request(
            f"{THETVDB_BASE}/search",
            params=params,
            headers=_thetvdb_headers(),
            rate_delay=THETVDB_RATE_LIMIT_DELAY,
        )
        return data.get("data", []) or []
    except Exception:
        return []


def thetvdb_fetch_episodes(series_id: int) -> list:
    """Fetch all regular episodes (seasonNumber > 0) for a TheTVDB series ID.

    TheTVDB paginates results. Page 0 may be entirely specials (season 0),
    so we must walk all pages and filter afterward.
    """
    if series_id in _thetvdb_episode_cache:
        return _thetvdb_episode_cache[series_id]

    episodes = []
    page = 0
    while True:
        try:
            data = _request(
                f"{THETVDB_BASE}/series/{series_id}/episodes/default",
                params={"page": page},
                headers=_thetvdb_headers(),
                rate_delay=THETVDB_RATE_LIMIT_DELAY,
            )
        except Exception as e:
            print(f"  [thetvdb] page {page} fetch error: {e}", file=sys.stderr)
            break

        inner = data.get("data", {})
        eps = inner.get("episodes", []) if isinstance(inner, dict) else []

        for ep in eps:
            s = ep.get("seasonNumber")
            e = ep.get("number")
            if not s or not e or s == 0:
                continue  # skip specials (season 0)
            airdate = ep.get("aired", "") or ""
            episodes.append(RemoteEpisode(
                season=s,
                episode=e,
                name=ep.get("name", "") or "",
                airdate=airdate,
            ))

        links = data.get("links", {})
        next_page = links.get("next")
        if not next_page:
            break
        page += 1

    _thetvdb_episode_cache[series_id] = episodes
    return episodes


# ---------------------------------------------------------------------------
# Show identification — pick the candidate whose episodes best fit local files
# ---------------------------------------------------------------------------
@dataclass
class Candidate:
    source: str                    # "tvmaze" or "thetvdb"
    series_id: int
    title: str
    names: List[str]               # title + aliases, for name matching
    status: Optional[str]
    year: Optional[int]
    rank: int                      # position in the source's search results
    episodes: list = field(default_factory=list)
    error: Optional[str] = None
    # scoring
    score: float = 0.0
    name_score: float = 0.0
    coverage: float = 0.0
    title_score: Optional[float] = None
    title_hits: int = 0            # local titles matching the same SxxEyy remotely
    title_moved: int = 0           # local titles found under a different number
    title_total: int = 0           # local files with an informative title
    pinned: str = ""               # set when chosen by an explicit ID (says where from)
    animation: Optional[bool] = None   # True/False when the source says; None if unknown
    page_url: str = ""             # show page, when it can't be derived from the ID

    @property
    def url(self) -> str:
        return self.page_url or SOURCES[self.source]["url"].format(id=self.series_id)
    year_ok: Optional[bool] = None

    def label(self) -> str:
        yr = f" ({self.year})" if self.year else ""
        return f"{self.title}{yr} [{self.source}]"


def _year_of(value: Any) -> Optional[int]:
    m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(value or ""))
    return int(m.group(1)) if m else None


def _tvmaze_candidate(show: dict, rank: int) -> Candidate:
    return Candidate(
        source="tvmaze", series_id=show["id"], title=show.get("name", ""),
        names=[show.get("name", "")], status=show.get("status"),
        year=_year_of(show.get("premiered")), rank=rank,
        animation=(show.get("type") == "Animation" or "Anime" in (show.get("genres") or []))
        if show.get("type") else None,
    )


def _thetvdb_candidate(show: dict, rank: int) -> Optional[Candidate]:
    series_id = show.get("tvdb_id") or show.get("id")
    try:
        series_id = int(str(series_id).replace("series-", ""))
    except (TypeError, ValueError):
        return None
    translations = show.get("translations") or {}
    eng = translations.get("eng") if isinstance(translations, dict) else None
    name = show.get("name") or eng or ""
    names = [name] + ([eng] if eng else []) + list(show.get("aliases") or [])
    status = show.get("status")
    if isinstance(status, dict):
        status = status.get("name")
    genres = show.get("genres")
    animation = None
    if isinstance(genres, list) and genres:
        gnames = [g.get("name", "") if isinstance(g, dict) else str(g) for g in genres]
        animation = any(g.lower() in ("animation", "anime") for g in gnames)
    return Candidate(
        source="thetvdb", series_id=series_id, title=eng or name,
        names=[n for n in names if isinstance(n, str) and n], status=status,
        year=_year_of(show.get("year") or show.get("first_air_time")), rank=rank,
        animation=animation,
    )


# ── TVmaze adapter ──────────────────────────────────────────────────────────
def _tvmaze_search(query: str, year: Optional[int]) -> List[Candidate]:
    return [_tvmaze_candidate(s, i) for i, s in enumerate(tvmaze_search_shows(query)[:MAX_CANDIDATES])]


def _tvmaze_fetch(cand: Candidate) -> None:
    cand.episodes = tvmaze_fetch_episodes(cand.series_id)


def _tvmaze_by_id(series_id: int) -> Candidate:
    return _tvmaze_candidate(_request(f"{TVMAZE_BASE}/shows/{series_id}"), 0)


# ── TheTVDB adapter ─────────────────────────────────────────────────────────
def _thetvdb_search(query: str, year: Optional[int]) -> List[Candidate]:
    raw = thetvdb_search_shows(query)[:MAX_CANDIDATES]
    if year:
        # Same-named shows can crowd the right one out of the top results
        # — keep the year-filtered results whose names fit best.
        def fit(d: dict) -> float:
            c = _thetvdb_candidate(d, 0)
            return max((difflib.SequenceMatcher(None, _norm(query), _base_title(n)).ratio()
                        for n in c.names), default=0.0) if c else 0.0
        dated = sorted(thetvdb_search_shows(query, year)[:15], key=fit, reverse=True)[:3]
        seen = {str(d.get("tvdb_id")) for d in dated}
        raw = dated + [r for r in raw if str(r.get("tvdb_id")) not in seen]
    found = [_thetvdb_candidate(s, i) for i, s in enumerate(raw)]
    return [c for c in found if c]


def _thetvdb_fetch(cand: Candidate) -> None:
    cand.episodes = thetvdb_fetch_episodes(cand.series_id)


def _thetvdb_by_id(series_id: int) -> Candidate:
    data = _request(
        f"{THETVDB_BASE}/series/{series_id}",
        headers=_thetvdb_headers(), rate_delay=THETVDB_RATE_LIMIT_DELAY,
    ).get("data") or {}
    data.setdefault("year", data.get("firstAired"))
    cand = _thetvdb_candidate(data, 0)
    if cand is None:
        raise RuntimeError(f"unexpected TheTVDB response for series {series_id}")
    return cand


# ── TMDB adapter (The Movie Database — strong on animation) ────────────────
_tmdb_key: Optional[str] = None
TMDB_ANIMATION_GENRE = 16


def _tmdb_auth() -> Tuple[Dict[str, Any], Dict[str, str]]:
    """(query params, headers) carrying the credential.

    Accepts either a v3 "API Key" (sent as ?api_key=) or a v4 "API Read Access
    Token" (a long JWT, sent as a Bearer header).
    """
    if not _tmdb_key:
        raise RuntimeError("TMDB key not initialized. Call tmdb_init() first.")
    if _tmdb_key.startswith("eyJ") or len(_tmdb_key) > 64:
        return {}, {"Authorization": f"Bearer {_tmdb_key}", "accept": "application/json"}
    return {"api_key": _tmdb_key}, {"accept": "application/json"}


def tmdb_init(api_key: str) -> None:
    """Store the TMDB credential and check it works."""
    global _tmdb_key
    _tmdb_key = api_key
    params, headers = _tmdb_auth()
    resp = requests.get(f"{TMDB_BASE}/configuration", params=params, headers=headers,
                        timeout=REQUEST_TIMEOUT)
    if resp.status_code == 401:
        _tmdb_key = None
        raise RuntimeError("TMDB rejected the key (HTTP 401)")
    resp.raise_for_status()


def _tmdb_get(path: str, **params: Any) -> dict:
    auth_params, headers = _tmdb_auth()
    return _request(f"{TMDB_BASE}{path}", params={**params, **auth_params},
                    headers=headers, rate_delay=TMDB_RATE_LIMIT_DELAY)


def _tmdb_candidate(show: dict, rank: int) -> Candidate:
    names = [show.get("name") or "", show.get("original_name") or ""]
    genre_ids = show.get("genre_ids")
    if genre_ids is None and isinstance(show.get("genres"), list):
        genre_ids = [g.get("id") for g in show["genres"]]
    return Candidate(
        source="tmdb", series_id=int(show["id"]), title=show.get("name") or show.get("original_name") or "",
        names=[n for n in dict.fromkeys(names) if n], status=show.get("status"),
        year=_year_of(show.get("first_air_date")), rank=rank,
        animation=(TMDB_ANIMATION_GENRE in genre_ids) if genre_ids else None,
    )


def _tmdb_search(query: str, year: Optional[int]) -> List[Candidate]:
    raw = (_tmdb_get("/search/tv", query=query, include_adult="false").get("results") or [])[:MAX_CANDIDATES]
    if year:
        dated = (_tmdb_get("/search/tv", query=query, include_adult="false",
                           first_air_date_year=year).get("results") or [])[:3]
        seen = {d.get("id") for d in dated}
        raw = dated + [r for r in raw if r.get("id") not in seen]
    return [_tmdb_candidate(s, i) for i, s in enumerate(raw) if s.get("id")]


def _tmdb_fetch(cand: Candidate) -> None:
    """Fetch every regular season (season 0 = specials is skipped)."""
    details = _tmdb_get(f"/tv/{cand.series_id}")
    cand.status = details.get("status") or cand.status
    seasons = sorted(
        s["season_number"] for s in details.get("seasons") or []
        if s.get("season_number") and s.get("episode_count")
    )
    episodes: List[RemoteEpisode] = []
    # Up to 20 seasons can ride along on one request via append_to_response
    for i in range(0, len(seasons), 20):
        chunk = seasons[i:i + 20]
        data = _tmdb_get(f"/tv/{cand.series_id}",
                         append_to_response=",".join(f"season/{n}" for n in chunk))
        for n in chunk:
            for ep in (data.get(f"season/{n}") or {}).get("episodes") or []:
                s_num, e_num = ep.get("season_number"), ep.get("episode_number")
                if not s_num or not e_num:
                    continue
                episodes.append(RemoteEpisode(
                    season=s_num, episode=e_num,
                    name=ep.get("name") or "", airdate=ep.get("air_date") or "",
                ))
    cand.episodes = episodes


def _tmdb_by_id(series_id: int) -> Candidate:
    return _tmdb_candidate(_tmdb_get(f"/tv/{series_id}"), 0)


# ── Episodate adapter (keyless; patchy data but covers many older cartoons) ─
def _episodate_candidate(show: dict, rank: int) -> Candidate:
    permalink = show.get("permalink") or ""
    return Candidate(
        source="episodate", series_id=int(show["id"]), title=show.get("name") or "",
        names=[show.get("name") or ""], status=show.get("status"),
        year=_year_of(show.get("start_date")), rank=rank,
        page_url=f"https://www.episodate.com/tv-show/{permalink}" if permalink else "",
    )


def _episodate_details(series_id: int) -> dict:
    data = _request(f"{EPISODATE_BASE}/show-details", params={"q": series_id},
                    rate_delay=EPISODATE_RATE_LIMIT_DELAY)
    show = data.get("tvShow") if isinstance(data, dict) else None
    if not isinstance(show, dict) or not show.get("id"):
        raise RuntimeError(f"Episodate has no show with ID {series_id}")
    return show


def _episodate_search(query: str, year: Optional[int]) -> List[Candidate]:
    data = _request(f"{EPISODATE_BASE}/search", params={"q": query, "page": 1},
                    rate_delay=EPISODATE_RATE_LIMIT_DELAY)
    shows = [s for s in (data.get("tv_shows") or []) if s.get("status") != "In Development"]
    return [_episodate_candidate(s, i) for i, s in enumerate(shows[:MAX_CANDIDATES]) if s.get("id")]


def _episodate_fetch(cand: Candidate) -> None:
    show = _episodate_details(cand.series_id)
    cand.status = show.get("status") or cand.status
    genres = show.get("genres")
    if cand.animation is None and isinstance(genres, list) and genres:
        cand.animation = any(str(g).lower() in ("animation", "anime", "cartoon") for g in genres) or None
    episodes = []
    for ep in show.get("episodes") or []:
        s_num, e_num = ep.get("season"), ep.get("episode")
        if not s_num or not e_num:
            continue
        episodes.append(RemoteEpisode(
            season=int(s_num), episode=int(e_num),
            name=ep.get("name") or "", airdate=(ep.get("air_date") or "")[:10],
        ))
    cand.episodes = episodes


def _episodate_by_id(series_id: int) -> Candidate:
    return _episodate_candidate(_episodate_details(series_id), 0)


# ── Source registry ─────────────────────────────────────────────────────────
SOURCES: Dict[str, Dict[str, Any]] = {
    "tvmaze": {
        "label": "TVmaze", "pin": "tvmaze", "key_flag": None,
        "url": "https://www.tvmaze.com/shows/{id}",
        "search": _tvmaze_search, "fetch": _tvmaze_fetch, "by_id": _tvmaze_by_id,
        "ready": lambda: True,
    },
    "thetvdb": {
        "label": "TheTVDB", "pin": "tvdb", "key_flag": "--thetvdb-apikey",
        "url": "https://www.thetvdb.com/dereferrer/series/{id}",
        "search": _thetvdb_search, "fetch": _thetvdb_fetch, "by_id": _thetvdb_by_id,
        "ready": lambda: bool(_thetvdb_token),
    },
    "tmdb": {
        "label": "TMDB", "pin": "tmdb", "key_flag": "--tmdb-apikey",
        "url": "https://www.themoviedb.org/tv/{id}",
        "search": _tmdb_search, "fetch": _tmdb_fetch, "by_id": _tmdb_by_id,
        "ready": lambda: bool(_tmdb_key),
    },
    "episodate": {
        "label": "Episodate", "pin": "episodate", "key_flag": None,
        "url": "https://www.episodate.com/search?q={id}",
        "search": _episodate_search, "fetch": _episodate_fetch, "by_id": _episodate_by_id,
        "ready": lambda: True,
    },
}
SOURCE_ALIASES = {"both": ["tvmaze", "thetvdb"]}


def source_label(name: str) -> str:
    return SOURCES[name]["label"] if name in SOURCES else name


def parse_sources(spec: str) -> List[str]:
    """"tvmaze,tmdb" / "both" / "all" → ordered list of source keys."""
    out: List[str] = []
    for part in (p.strip().lower() for p in spec.split(",")):
        if not part:
            continue
        if part == "all":
            names = list(SOURCES)
        elif part in SOURCE_ALIASES:
            names = SOURCE_ALIASES[part]
        elif part == "tvdb":
            names = ["thetvdb"]
        elif part in SOURCES:
            names = [part]
        else:
            raise ValueError(
                f"unknown source {part!r} (choose from: {', '.join(SOURCES)}, both, all)"
            )
        out.extend(n for n in names if n not in out)
    if not out:
        raise ValueError("no source given")
    return out


# Cartoons often pack several segments into one episode: "Dee Dee's Room / Star Spangled Sidekicks"
_SEGMENT_SPLIT_RE = re.compile(r"\s+/\s+|\s*;\s+|\s+\+\s+|\s+&\s+(?=[A-Z])|\s+-\s+")


def _title_variants(title: str) -> List[str]:
    """Normalised whole title plus each of its segments (deduplicated, generic ones dropped)."""
    out = [_norm(title)]
    parts = [p for p in _SEGMENT_SPLIT_RE.split(title or "") if p.strip()]
    if len(parts) > 1:
        out += [_norm(p) for p in parts if not _is_generic_title(p)]
    return [v for v in dict.fromkeys(out) if v]


def _local_titles(local: list) -> List[Tuple[List[str], List[Tuple[int, int]]]]:
    """[(title variants, [(season, episode), …])] — one entry per titled file."""
    by_path: Dict[Path, Tuple[List[str], List[Tuple[int, int]]]] = {}
    for ep in local:
        if ep.episode == 0 or _is_generic_title(ep.title):
            continue
        by_path.setdefault(ep.path, (_title_variants(ep.title), []))[1].append((ep.season, ep.episode))
    return list(by_path.values())


def _score_titles(local_titles: list, remote: list) -> Tuple[float, int, int]:
    """Compare local episode titles with a candidate's episode names.

    Full credit when the title matches the remote episode with the same
    season/episode number; partial credit when it matches a different episode
    of the same show (alternate ordering, e.g. DVD vs aired). Multi-segment
    episodes match on any one segment, in either direction.
    Returns (score 0..1, same-slot hits, found-elsewhere hits).
    """
    by_slot: Dict[Tuple[int, int], List[str]] = {}
    by_name: Set[str] = set()
    by_word: Dict[str, Set[str]] = {}
    for rep in remote:
        variants = _title_variants(rep.name)
        if not variants:
            continue
        by_slot.setdefault((rep.season, rep.episode), variants)
        for n in variants:
            if _GENERIC_TITLE_RE.match(n):
                continue
            by_name.add(n)
            for w in n.split():
                if len(w) > 3:
                    by_word.setdefault(w, set()).add(n)

    def best_sim(mine: List[str], theirs) -> float:
        return max((_title_similarity(a, b) for a in mine for b in theirs), default=0.0)

    total, hits, moved = 0.0, 0, 0
    for variants, slots in local_titles:
        same = max((best_sim(variants, by_slot.get(sl, ())) for sl in slots), default=0.0)
        if same >= TITLE_HIT:
            hits += 1
            total += same
            continue
        # Look for the title anywhere else in this show
        if any(v in by_name for v in variants):
            other = 1.0
        else:
            pool: Set[str] = set()
            for v in variants:
                for w in v.split():
                    pool |= by_word.get(w, set())
            other = best_sim(variants, pool)
        if other >= TITLE_HIT:
            moved += 1
        best = max(same, other * 0.7)
        total += best if max(same, other) >= TITLE_FLOOR else 0.0
    return (total / len(local_titles) if local_titles else 0.0), hits, moved


def _score_candidate(
    cand: Candidate,
    local: list,
    local_titles: list,
    folder_name: str,
    folder_year: Optional[int],
) -> None:
    """Fill in cand.score from name, year, episode numbering and episode titles."""
    today = time.strftime("%Y-%m-%d")
    fn = _norm(folder_name)
    cand.name_score = max(
        (max(difflib.SequenceMatcher(None, fn, _norm(n)).ratio(),
             difflib.SequenceMatcher(None, fn, _base_title(n)).ratio()) for n in cand.names),
        default=0.0,
    )

    local_known = {(ep.season, ep.episode) for ep in local if ep.episode != 0}
    remote_set = {(ep.season, ep.episode) for ep in cand.episodes}
    cand.coverage = (
        len(local_known & remote_set) / len(local_known) if local_known and remote_set else 0.0
    )
    aired = sum(1 for ep in cand.episodes if ep.airdate and ep.airdate <= today)
    count_fit = (
        min(len(local_known), aired) / max(len(local_known), aired)
        if local_known and aired else 0.0
    )
    rank_score = max(0.0, 1.0 - cand.rank / MAX_CANDIDATES)

    # Episode-count fit is deliberately weak: incomplete libraries are the norm here.
    base = 0.35 * cand.coverage + 0.05 * count_fit + 0.50 * cand.name_score + 0.10 * rank_score
    if cand.name_score >= 0.999:
        base += 0.05                      # exact name beats a near-miss ("Kingdom" vs "Kingdoms")

    cand.title_total = len(local_titles)
    if local_titles and cand.episodes:
        cand.title_score, cand.title_hits, cand.title_moved = _score_titles(
            local_titles, cand.episodes
        )
        # Episode titles are the strongest evidence; trust them more the more we have
        w = 0.65 * min(1.0, len(local_titles) / 3)
        score = w * cand.title_score + (1 - w) * base
    else:
        score = base

    if folder_year and cand.year:
        diff = abs(folder_year - cand.year)
        cand.year_ok = diff <= 1
        score += 0.15 if diff == 0 else 0.05 if diff == 1 else -0.25
    if PREFER_ANIMATION and cand.animation is not None:
        score += 0.08 if cand.animation else -0.08
    if not cand.episodes:
        score -= 0.5
    cand.score = score        # unclamped, so close calls still rank correctly


def _is_confident(cand: Candidate) -> bool:
    """Good enough that lower-ranked results from the same source can be skipped."""
    return (
        cand.name_score >= 0.9
        and cand.year_ok is not False
        and cand.title_score is not None
        and cand.title_total >= 3
        and cand.title_score >= 0.85
    )


def _fetch_candidate_episodes(cand: Candidate) -> None:
    try:
        SOURCES[cand.source]["fetch"](cand)
    except Exception as exc:
        cand.episodes = []
        cand.error = str(exc)


def _pinned_candidate(src: str, series_id: int) -> Candidate:
    """Fetch a show by explicit ID, bypassing search."""
    info = SOURCES[src]
    spec = f"{info['pin']}:{series_id}"
    if not info["ready"]():
        raise RuntimeError(f"override {spec} needs {info['label']} access — pass {info['key_flag']}")
    try:
        return info["by_id"](series_id)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "?"
        raise RuntimeError(f"override {spec} could not be fetched (HTTP {status})")
    except RuntimeError as exc:
        raise RuntimeError(f"override {spec} could not be fetched ({exc})")


def identify_show(show_name: str, local: list, sources: List[str]) -> List[Candidate]:
    """Return scored candidates for a show folder, best first (may be empty)."""
    query, folder_year = split_show_year(show_name)
    local_titles = _local_titles(local)

    # An explicit ID from the overrides file skips searching
    pin = OVERRIDES.get(show_name)
    if pin:
        cand = _pinned_candidate(*pin)
        cand.pinned = "overrides file"
        _fetch_candidate_episodes(cand)
        _score_candidate(cand, local, local_titles, query, folder_year)
        return [cand]

    evaluated: List[Candidate] = []
    for src in sources:
        search = SOURCES[src]["search"]
        try:
            cands = search(query, folder_year)
            if not cands and query != show_name:
                cands = search(show_name, None)   # the "year" may be part of the real name
        except Exception as exc:
            print(f"  [{src}] search failed for {show_name!r}: {exc}", file=sys.stderr)
            continue

        for cand in cands:
            _fetch_candidate_episodes(cand)
            _score_candidate(cand, local, local_titles, query, folder_year)
            evaluated.append(cand)
            if _is_confident(cand):
                break                        # no need to fetch lower-ranked results

    evaluated.sort(key=lambda c: (-c.score, c.rank))
    return evaluated


def _base_title(title: str) -> str:
    """Normalised title without a trailing qualifier: "The Office (US)" → "the office"."""
    return _norm(re.sub(r"\s*\([^)]*\)\s*$", "", title or "")) or _norm(title)


def _same_show(a: Candidate, b: Candidate) -> bool:
    """True if two candidates are one series as listed by different sources."""
    if a.source == b.source or _base_title(a.title) != _base_title(b.title):
        return False
    return not (a.year and b.year) or abs(a.year - b.year) <= 2


def _describe_match(best: Candidate, others: List[Candidate]) -> Tuple[str, str]:
    """Return (note, flag) explaining why `best` was chosen."""
    parts = []
    if best.title_score is not None:
        part = f"{best.title_hits}/{best.title_total} episode titles matched"
        if best.title_moved:
            part += f" (+{best.title_moved} found under a different episode number)"
        parts.append(part)
    else:
        parts.append("no episode titles in filenames — matched on show name and numbering")
    parts.append(f"name similarity {best.name_score:.0%}")
    if best.year_ok is False:
        parts.append("year differs from folder name")

    flag = ""
    # A runner-up only makes the match ambiguous if it is a *different* show,
    # not the same series as listed by the other metadata source.
    rivals = [c for c in others if not _same_show(c, best)]
    if best.score < LOW_CONFIDENCE or best.year_ok is False:
        flag = "low"
    elif rivals and best.score - rivals[0].score < AMBIGUOUS_MARGIN:
        flag = "ambiguous"
    return "; ".join(parts), flag


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------
def compare(local: list, remote: list, only_aired: bool = True) -> tuple:
    local_set = {(ep.season, ep.episode) for ep in local if ep.episode != 0}
    today = time.strftime("%Y-%m-%d")

    missing = []
    for rep in remote:
        if only_aired and rep.airdate and rep.airdate > today:
            continue
        if only_aired and not rep.airdate:
            continue
        if (rep.season, rep.episode) not in local_set:
            missing.append(rep)

    remote_set = {(ep.season, ep.episode) for ep in remote}
    extra = [ep for ep in local if ep.episode != 0 and (ep.season, ep.episode) not in remote_set]

    return missing, extra


# ---------------------------------------------------------------------------
# Per-show worker
# ---------------------------------------------------------------------------
def _error_report(show_name: str, local_eps: list, error: str,
                  cand: Optional[Candidate] = None) -> ShowReport:
    return ShowReport(
        show_name=show_name,
        matched_title=cand.title if cand else None,
        tvmaze_id=cand.series_id if cand and cand.source == "tvmaze" else None,
        status=cand.status if cand else None,
        local_seasons=set(), local_episodes=local_eps,
        remote_episodes=[], missing=[], extra=[],
        lookup_error=error, source_used=cand.source if cand else "none",
        source_id=cand.series_id if cand else None,
        source_url=cand.url if cand else "",
    )


def process_show(show_name: str, local_eps: list, sources: List[str]) -> ShowReport:
    try:
        candidates = identify_show(show_name, local_eps, sources)
        if not candidates:
            names = " or ".join(source_label(s) for s in sources)
            return _error_report(show_name, local_eps, f"No match found on {names}")

        best, others = candidates[0], candidates[1:]
        if not best.episodes:
            return _error_report(
                show_name, local_eps,
                f"No episodes found on {source_label(best.source)}"
                + (f" ({best.error})" if best.error else ""),
                best,
            )

        note, flag = _describe_match(best, others)
        confidence = max(0.0, min(1.0, best.score))
        if best.pinned:
            note, flag, confidence = f"pinned by {best.pinned}; {note}", "", 1.0
        # Several shows share this name — say which one was picked
        matched_title = best.title or show_name
        if best.year and not matched_title.rstrip().endswith(")") and any(
            _base_title(c.title) == _base_title(best.title) and not _same_show(c, best)
            for c in others
        ):
            matched_title = f"{matched_title} ({best.year})"
        missing, extra = compare(local_eps, best.episodes)
        tvmaze_id = next((c.series_id for c in candidates if c.source == "tvmaze"
                          and (c is best or _same_show(c, best))), None)
        return ShowReport(
            show_name=show_name,
            matched_title=matched_title,
            tvmaze_id=tvmaze_id,
            status=best.status or "Unknown",
            local_seasons={ep.season for ep in local_eps},
            local_episodes=local_eps,
            remote_episodes=best.episodes,
            missing=missing,
            extra=extra,
            lookup_error=None,
            source_used=best.source,
            source_id=best.series_id,
            source_url=best.url,
            match_confidence=confidence,
            match_note=note,
            match_flag=flag,
            alternatives=[
                {"title": c.title, "year": c.year, "source": c.source, "id": c.series_id, "url": c.url,
                 "score": round(max(0.0, min(1.0, c.score)), 3)}
                for c in others[:4]
            ],
        )

    except Exception as exc:
        return _error_report(show_name, local_eps, str(exc))


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------
def _h(text: str) -> str:
    """HTML-escape a string."""
    return (str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------
def _season_ep(ep) -> str:
    return f"S{ep.season:02d}E{ep.episode:02d}"


def format_text(reports: list, missing_only: bool) -> str:
    lines = []
    ok_count = sum(1 for r in reports if r.ok)
    issue_count = len(reports) - ok_count

    lines.append(_bold(f"TV Library Report — {len(reports)} shows scanned"))
    lines.append("─" * 60)
    lines.append(f"  {_green('✓')} Complete:  {_green(str(ok_count))}")
    lines.append(f"  {_red('✗')} Issues:    {_red(str(issue_count))}")
    lines.append("")

    for report in sorted(reports, key=lambda r: r.show_name.lower()):
        if missing_only and report.ok:
            continue

        source_label = report.source_used.upper()
        title_part = _bold(_cyan(f"► {report.show_name}"))
        if report.matched_title and report.matched_title != report.show_name:
            title_part += _dim(f"  [{source_label}: {report.matched_title}]")
        if report.status:
            title_part += f"  {_dim(f'({report.status})')}"
        lines.append(title_part)

        if report.lookup_error:
            lines.append(f"    {_yellow('⚠')} {_yellow(report.lookup_error)}")
            lines.append("")
            continue

        if report.match_flag:
            alts = ", ".join(
                f"{a['title']}" + (f" ({a['year']})" if a["year"] else "") + f" {a['score']:.0%}"
                for a in report.alternatives[:3]
            )
            why = "Low-confidence match" if report.match_flag == "low" else "Ambiguous match"
            lines.append(
                f"    {_yellow('⚠')} {_yellow(f'{why} ({report.match_confidence:.0%})')}"
                f" {_dim('— ' + report.match_note)}"
            )
            if alts:
                lines.append(f"      {_dim('Also considered: ' + alts)}")

        local_count = len({(e.season, e.episode) for e in report.local_episodes if e.episode != 0})
        _today = time.strftime("%Y-%m-%d")
        remote_count = sum(1 for e in report.remote_episodes if e.airdate and e.airdate <= _today)
        lines.append(
            f"    Local: {local_count} episodes  |  "
            f"{source_label} (aired): {remote_count} episodes"
        )

        if not report.missing and not report.extra:
            lines.append(f"    {_green('✓ All aired episodes present')}")
        else:
            if report.missing:
                by_season: dict = {}
                for ep in report.missing:
                    by_season.setdefault(ep.season, []).append(ep)

                local_seasons_with_eps = {
                    ep.season for ep in report.local_episodes if ep.episode != 0
                }

                today = time.strftime("%Y-%m-%d")
                aired_by_season: dict = {}
                for ep in report.remote_episodes:
                    if ep.airdate and ep.airdate <= today:
                        aired_by_season[ep.season] = aired_by_season.get(ep.season, 0) + 1

                for s in sorted(by_season):
                    missing_eps = by_season[s]
                    if s not in local_seasons_with_eps:
                        count_str = f"{len(missing_eps)} episode{'s' if len(missing_eps) != 1 else ''}"
                        lines.append(
                            f"    {_red('✗')} {_red(f'Season {s:02d} entirely missing')} "
                            f"{_dim(f'({count_str})')}"
                        )
                    else:
                        ep_nums = ", ".join(
                            _season_ep(e)
                            for e in sorted(missing_eps, key=lambda x: x.episode)
                        )
                        lines.append(f"    {_red('✗')} Missing {_red(f'S{s:02d}')}: {ep_nums}")

            if report.extra:
                ep_list = ", ".join(
                    _season_ep(e)
                    for e in sorted(report.extra, key=lambda x: (x.season, x.episode))
                )
                lines.append(f"    {_yellow('?')} Extra (not in {source_label}): {_yellow(ep_list)}")

        lines.append("")

    return "\n".join(lines)


def format_json(reports: list) -> str:
    def rep_to_dict(r: ShowReport) -> dict:
        return {
            "show_name": r.show_name,
            "matched_title": r.matched_title,
            "tvmaze_id": r.tvmaze_id,
            "status": r.status,
            "source_used": r.source_used,
            "source_id": r.source_id,
            "source_url": r.source_url,
            "match_confidence": round(r.match_confidence, 3) if r.match_confidence is not None else None,
            "match_note": r.match_note,
            "match_flag": r.match_flag,
            "alternatives": r.alternatives,
            "local_episode_count": len(r.local_episodes),
            "remote_episode_count": len(r.remote_episodes),
            "ok": r.ok,
            "lookup_error": r.lookup_error,
            "missing": [
                {"season": e.season, "episode": e.episode, "name": e.name, "airdate": e.airdate}
                for e in r.missing
            ],
            "extra": [
                {"season": e.season, "episode": e.episode, "path": str(e.path)}
                for e in r.extra
            ],
        }
    return json.dumps([rep_to_dict(r) for r in reports], indent=2)


_HTML_CSS = """
  :root {
    --bg: #0f1117; --surface: #1a1d27; --border: #2a2d3a;
    --text: #e2e8f0; --strong: #f1f5f9; --dim: #7c8aa0;
    --accent: #818cf8; --green: #22c55e; --red: #ef4444; --yellow: #f59e0b;
    --tag-bg: #2a2d3a; --miss-bg: #ef444422; --miss-fg: #fca5a5;
    --extra-bg: #f59e0b22; --extra-fg: #fcd34d; --tip-bg: #1e2130;
    --shadow: 0 8px 32px rgba(0,0,0,.6);
  }
  @media (prefers-color-scheme: light) {
    :root {
      --bg: #f8fafc; --surface: #ffffff; --border: #e2e8f0;
      --text: #1e293b; --strong: #0f172a; --dim: #64748b;
      --accent: #4f46e5; --green: #15803d; --red: #dc2626; --yellow: #b45309;
      --tag-bg: #e2e8f0; --miss-bg: #fee2e2; --miss-fg: #b91c1c;
      --extra-bg: #fef3c7; --extra-fg: #92400e; --tip-bg: #ffffff;
      --shadow: 0 8px 24px rgba(15,23,42,.18);
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px; padding: 2rem;
  }
  h1 { font-size: 1.4rem; font-weight: 700; margin-bottom: .25rem; color: var(--strong); }
  .meta { color: var(--dim); font-size: .8rem; margin-bottom: 1.5rem; line-height: 1.5; }
  .meta code { font-size: .78rem; }
  .summary {
    display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
    gap: .75rem; margin-bottom: 1.5rem;
  }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: .75rem 1rem; }
  .card .label { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); }
  .card .value { font-size: 1.2rem; font-weight: 700; margin-top: .2rem; font-variant-numeric: tabular-nums; }
  .card .sub { font-size: .72rem; color: var(--dim); margin-top: .1rem; }
  .toolbar { display: flex; align-items: center; gap: 1rem; margin-bottom: .75rem; flex-wrap: wrap; }
  input#search {
    background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
    color: var(--text); padding: .4rem .8rem; font-size: .85rem; outline: none; width: 260px;
  }
  input#search:focus { border-color: var(--accent); }
  label.filter-label { color: var(--dim); font-size: .8rem; display: flex; align-items: center; gap: .35rem; cursor: pointer; }
  #row-count { color: var(--dim); font-size: .8rem; margin-left: auto; }
  .tbtn {
    background: none; border: 1px solid var(--border); border-radius: 6px; color: var(--dim);
    font: inherit; font-size: .78rem; padding: .3rem .6rem; cursor: pointer;
  }
  .tbtn:hover { color: var(--text); border-color: var(--dim); }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th {
    background: var(--surface); color: var(--dim); font-weight: 600; text-transform: uppercase;
    font-size: .68rem; letter-spacing: .04em; padding: .55rem .6rem;
    border-bottom: 1px solid var(--border); text-align: left;
    position: sticky; top: 0; z-index: 2; user-select: none; white-space: nowrap;
  }
  thead th.sortable { cursor: pointer; }
  thead th.sortable:hover { color: var(--text); }
  thead th.sorted { color: var(--accent); }
  .arrow { display: inline-block; width: .9em; font-size: .8em; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  thead th.num { text-align: right; }
  tbody tr.main { border-top: 1px solid var(--border); }
  tbody tr.main:hover { background: var(--surface); }
  td { padding: .5rem .6rem; vertical-align: top; }
  .ok-col { width: 24px; text-align: center; font-weight: 700; }
  .row-ok .ok-col { color: var(--green); }
  .row-issue .ok-col { color: var(--red); }
  .show-name { min-width: 200px; }
  .show-btn {
    background: none; border: 0; padding: 0; font: inherit; font-weight: 600;
    color: var(--strong); cursor: pointer; text-align: left; display: flex; gap: .35rem;
  }
  .show-btn:hover, .show-btn:focus-visible { color: var(--accent); outline: none; }
  .chev { display: inline-block; color: var(--dim); transition: transform .12s; width: .8em; flex: none; }
  .show-btn[aria-expanded="true"] .chev { transform: rotate(90deg); }
  .matched-title { display: block; font-size: .75rem; font-weight: 400; margin: .15rem 0 0 1.15em; }
  .dim { color: var(--dim); }
  .badge {
    display: inline-block; padding: .15rem .5rem; border-radius: 999px; font-size: .7rem;
    font-weight: 600; text-transform: uppercase; letter-spacing: .04em; white-space: nowrap; cursor: default;
  }
  .source-badge { font-size: .65rem; }
  .eps-col { min-width: 96px; }
  .eps-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
  .bar { height: 4px; border-radius: 2px; background: var(--tag-bg); margin-top: .3rem; overflow: hidden; }
  .bar span { display: block; height: 100%; background: var(--green); border-radius: 2px; }
  .bar.partial span { background: var(--yellow); }
  .miss-count { color: var(--red); font-weight: 700; }
  .detail-col { max-width: 560px; line-height: 1.7; }
  .miss-x { color: var(--red); font-weight: 700; margin-right: .3rem; }
  .extra-q { color: var(--yellow); font-weight: 700; margin-right: .3rem; }
  .warn { color: var(--yellow); }
  .all-ok { color: var(--green); }
  .ep-tag {
    display: inline-block; border-radius: 4px; padding: 0 .35rem; margin: 0 .1rem;
    font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: .72rem;
    background: var(--miss-bg); color: var(--miss-fg); cursor: default; white-space: nowrap;
  }
  .ep-tag.extra { background: var(--extra-bg); color: var(--extra-fg); }
  .ep-tag.have { background: var(--tag-bg); color: var(--text); }
  /* Expandable per-show panel */
  tr.panel-row > td { padding: 0 .6rem .9rem; }
  .panel {
    background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
    padding: .8rem 1rem; display: grid; gap: .9rem 2rem;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); line-height: 1.6;
  }
  .panel h4 { font-size: .68rem; text-transform: uppercase; letter-spacing: .05em; color: var(--dim); margin-bottom: .3rem; }
  .panel a { color: var(--accent); text-decoration: none; }
  .panel a:hover { text-decoration: underline; }
  .panel code, .meta code {
    font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace; font-size: .74rem;
    background: var(--tag-bg); border-radius: 4px; padding: .05rem .35rem; user-select: all;
  }
  .panel ul { list-style: none; }
  .panel li { margin: .15rem 0; }
  .panel .path { color: var(--dim); font-size: .74rem; word-break: break-all; }
  .panel .hint { color: var(--dim); font-size: .76rem; margin-top: .35rem; }
  footer { margin-top: 2rem; color: var(--dim); font-size: .75rem; }
  .tooltip-box {
    position: fixed; z-index: 9999; background: var(--tip-bg); border: 1px solid var(--border);
    border-radius: 8px; padding: .6rem .85rem; font-size: .78rem; line-height: 1.6; color: var(--text);
    max-width: 460px; white-space: pre-wrap; overflow-wrap: anywhere; box-shadow: var(--shadow);
    pointer-events: none; opacity: 0; transition: opacity .12s;
  }
  .tooltip-box.visible { opacity: 1; }
  .has-tip { cursor: help; }
  thead th.has-tip { cursor: pointer; }
  .th-label { border-bottom: 1px dotted var(--dim); }
  @media print {
    body { padding: 0; }
    .toolbar, .tooltip-box { display: none; }
    thead th { position: static; }
  }
"""

_HTML_JS = """
  // ── Tooltip ──────────────────────────────────────────────────────────────
  const tt = document.getElementById('tt');
  let ttVisible = false;
  document.addEventListener('mouseover', e => {
    const el = e.target.closest('.has-tip');
    if (!el || !el.dataset.tip) return;
    tt.textContent = el.dataset.tip;
    tt.classList.add('visible');
    ttVisible = true;
  });
  document.addEventListener('mouseout', e => {
    if (!e.target.closest('.has-tip')) return;
    tt.classList.remove('visible');
    ttVisible = false;
  });
  document.addEventListener('mousemove', e => {
    if (!ttVisible) return;
    const pad = 16;
    let x = e.clientX + pad, y = e.clientY + pad;
    const bw = tt.offsetWidth, bh = tt.offsetHeight;
    if (x + bw > window.innerWidth  - pad) x = Math.max(pad, e.clientX - bw - pad);
    if (y + bh > window.innerHeight - pad) y = Math.max(pad, e.clientY - bh - pad);
    tt.style.left = x + 'px';
    tt.style.top  = y + 'px';
  });

  // ── Rows: each show is a main row followed by its (hidden) panel row ─────
  const tbody = document.getElementById('main-tbody');
  const mains = () => Array.from(tbody.querySelectorAll('tr.main'));
  const panelOf = row => row.nextElementSibling;

  function setOpen(row, open) {
    panelOf(row).hidden = !open;
    row.querySelector('.show-btn').setAttribute('aria-expanded', open ? 'true' : 'false');
  }
  tbody.addEventListener('click', e => {
    const btn = e.target.closest('.show-btn');
    if (!btn) return;
    const row = btn.closest('tr.main');
    setOpen(row, panelOf(row).hidden);
  });
  document.getElementById('toggle-all').addEventListener('click', e => {
    const visible = mains().filter(r => !r.hidden);
    const open = visible.some(r => panelOf(r).hidden);
    visible.forEach(r => setOpen(r, open));
    e.target.textContent = open ? 'Collapse all' : 'Expand all';
  });

  // ── Filter ───────────────────────────────────────────────────────────────
  const search = document.getElementById('search');
  const issuesOnly = document.getElementById('issues-only');
  const flaggedOnly = document.getElementById('flagged-only');
  function applyFilter() {
    const q = search.value.trim().toLowerCase();
    let shown = 0;
    const all = mains();
    all.forEach(row => {
      const ok = row.dataset.search.includes(q)
        && (!issuesOnly.checked || row.dataset.issue === '1')
        && (!flaggedOnly.checked || row.dataset.flag === '1');
      row.hidden = !ok;
      if (!ok) panelOf(row).hidden = true;
      else { shown++; panelOf(row).hidden = row.querySelector('.show-btn').getAttribute('aria-expanded') !== 'true'; }
    });
    document.getElementById('row-count').textContent =
      shown === all.length ? all.length + ' shows' : 'Showing ' + shown + ' of ' + all.length + ' shows';
  }
  [search, issuesOnly, flaggedOnly].forEach(el => el.addEventListener('input', applyFilter));

  // ── Sort ─────────────────────────────────────────────────────────────────
  let sortCol = 1, sortAsc = true;
  const numCols = new Set([0, 4, 5]);
  function sortVal(row, col) {
    const cell = row.cells[col];
    return cell.dataset.sort !== undefined ? cell.dataset.sort : cell.innerText.trim();
  }
  function markSorted() {
    document.querySelectorAll('thead th').forEach((th, i) => {
      th.classList.toggle('sorted', i === sortCol);
      const a = th.querySelector('.arrow');
      if (a) a.textContent = i === sortCol ? (sortAsc ? '▲' : '▼') : '';
    });
  }
  function sortTable(col) {
    if (sortCol === col) sortAsc = !sortAsc;
    else { sortCol = col; sortAsc = !numCols.has(col) || col === 4; }
    const rows = mains().map(r => [r, panelOf(r)]);
    rows.sort((a, b) => {
      let av = sortVal(a[0], col), bv = sortVal(b[0], col), d;
      if (numCols.has(col)) d = (parseFloat(av) || 0) - (parseFloat(bv) || 0);
      else d = av.localeCompare(bv, undefined, {sensitivity: 'base', numeric: true});
      if (d === 0) d = sortVal(a[0], 1).localeCompare(sortVal(b[0], 1), undefined, {sensitivity: 'base'});
      return sortAsc ? d : -d;
    });
    rows.forEach(([r, p]) => { tbody.appendChild(r); tbody.appendChild(p); });
    markSorted();
  }
  document.querySelectorAll('thead th.sortable').forEach(th =>
    th.addEventListener('click', () => sortTable(th.cellIndex)));
  markSorted();
  applyFilter();
"""


def _runs(nums) -> List[Tuple[int, int]]:
    """[1,2,3,5,7,8] → [(1,3), (5,5), (7,8)]"""
    out: List[List[int]] = []
    for n in sorted(set(nums)):
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return [(a, b) for a, b in out]


def _pin_line(folder: str, source: str, series_id: int) -> str:
    """A ready-to-paste overrides-file entry."""
    prefix = SOURCES[source]["pin"] if source in SOURCES else source
    return f"{json.dumps(folder, ensure_ascii=False)}: {json.dumps(f'{prefix}:{series_id}')}"


def format_html(reports: list, missing_only: bool, meta: Optional[dict] = None) -> str:
    from datetime import datetime

    meta = meta or {}
    today = time.strftime("%Y-%m-%d")

    def aired_count(r) -> int:
        return sum(1 for e in r.remote_episodes if e.airdate and e.airdate <= today)

    def known(r) -> set:
        return {(e.season, e.episode) for e in r.local_episodes if e.episode != 0}

    def unparsed(r) -> List[str]:
        return sorted({e.path.name for e in r.local_episodes if e.episode == 0})

    ok_count = sum(1 for r in reports if r.ok)
    issue_count = len(reports) - ok_count
    flagged_count = sum(1 for r in reports if r.match_flag)
    unmatched_count = sum(1 for r in reports if r.lookup_error)
    total_local = sum(len(known(r)) for r in reports)
    total_missing = sum(len(r.missing) for r in reports)
    total_aired = sum(aired_count(r) for r in reports)
    overall_pct = (total_aired - total_missing) / total_aired if total_aired else 0.0

    STATUS_COLORS = {
        "Ended":            "#64748b",
        "Continuing":       "#22c55e",
        "Running":          "#22c55e",
        "To Be Determined": "#f59e0b",
        "Returning Series": "#22c55e",
        "Canceled":         "#64748b",
        "Canceled/Ended":   "#64748b",
        "Upcoming":         "#6366f1",
        "In Development":   "#6366f1",
    }
    SOURCE_COLORS = {"tvmaze": "#6366f1", "thetvdb": "#f59e0b", "tmdb": "#0ea5e9", "episodate": "#10b981"}
    STATUS_DESC = {
        "Ended":            "This show has finished airing — no new episodes expected.",
        "Continuing":       "This show is still airing — new episodes may appear.",
        "Running":          "This show is still airing — new episodes may appear.",
        "To Be Determined": "Renewal status is unknown.",
        "Returning Series": "This show is still airing — new episodes may appear.",
        "Canceled":         "This show was cancelled — no new episodes expected.",
        "Canceled/Ended":   "This show has finished airing — no new episodes expected.",
        "Upcoming":         "This show has not yet started airing.",
        "In Development":   "This show has not yet started airing.",
    }
    SOURCE_DESC = {
        "tvmaze":  "Episode data sourced from TVmaze (tvmaze.com). No API key required.",
        "thetvdb": "Episode data sourced from TheTVDB (thetvdb.com). Requires an API key.",
        "tmdb":    "Episode data sourced from The Movie Database (themoviedb.org). Requires an API key.",
        "episodate": "Episode data sourced from Episodate (episodate.com). No API key required.",
        "none":    "No database returned a usable match for this folder.",
    }

    def badge(text: str, colour: str, tip: str, extra_class: str = "") -> str:
        return (f'<span class="badge {extra_class} has-tip" style="background:{colour}22;'
                f'color:{colour};border:1px solid {colour}55" data-tip="{_h(tip)}">{_h(text)}</span>')

    def ep_label(a: int, b: int) -> str:
        return f"E{a:02d}" if a == b else f"E{a:02d}–E{b:02d}"

    def missing_tag(season: int, a: int, b: int, by_num: dict) -> str:
        tips = []
        for n in range(a, b + 1):
            ep = by_num.get(n)
            line = f"S{season:02d}E{n:02d}"
            if ep and ep.name:
                line += f"  {ep.name}"
            if ep and ep.airdate:
                line += f"  ({ep.airdate})"
            tips.append(line)
        if len(tips) > 14:
            tips = tips[:13] + [f"… and {len(tips) - 13} more"]
        return f'<span class="ep-tag has-tip" data-tip="{_h(chr(10).join(tips))}">{ep_label(a, b)}</span>'

    def build_detail(r) -> str:
        if r.lookup_error:
            return f'<span class="warn">⚠ {_h(r.lookup_error)}</span>'
        parts = []
        if r.missing:
            by_season: Dict[int, list] = {}
            for ep in r.missing:
                by_season.setdefault(ep.season, []).append(ep)
            have_seasons = {s for s, _ in known(r)}

            # Partially-present seasons: list the gaps as episode ranges
            for s in sorted(by_season):
                if s not in have_seasons:
                    continue
                by_num = {e.episode: e for e in by_season[s]}
                tags = " ".join(missing_tag(s, a, b, by_num) for a, b in _runs(by_num))
                parts.append(f'<div><span class="miss-x">✗</span>Missing <strong>S{s:02d}</strong>: {tags}'
                             f' <span class="dim">({len(by_num)})</span></div>')

            # Seasons with nothing on disk: merge consecutive seasons into one line
            absent = [s for s in sorted(by_season) if s not in have_seasons]
            for a, b in _runs(absent):
                n = sum(len(by_season[s]) for s in range(a, b + 1) if s in by_season)
                label = f"S{a:02d}" if a == b else f"S{a:02d}–S{b:02d}"
                tip = "\n".join(f"Season {s:02d}: {len(by_season[s])} episodes" for s in range(a, b + 1))
                parts.append(f'<div class="has-tip" data-tip="{_h(tip)}"><span class="miss-x">✗</span>'
                             f'<strong>{label}</strong> entirely missing '
                             f'<span class="dim">({n} ep{"s" if n != 1 else ""})</span></div>')
        if r.extra:
            seen = set()
            tags = []
            for e in sorted(r.extra, key=lambda x: (x.season, x.episode)):
                if (e.season, e.episode) in seen:
                    continue
                seen.add((e.season, e.episode))
                tags.append(f'<span class="ep-tag extra has-tip" data-tip="{_h(e.path.name)}">'
                            f'S{e.season:02d}E{e.episode:02d}</span>')
            parts.append(f'<div class="has-tip" data-tip="On disk, but {_h(r.source_used.upper())} lists no '
                         f'such episode — a special, a different numbering scheme, or a wrong match.">'
                         f'<span class="extra-q">?</span>Not in {_h(r.source_used.upper())}: {" ".join(tags)}</div>')
        files = unparsed(r)
        if files:
            tip = "\n".join(files[:15]) + (f"\n… and {len(files) - 15} more" if len(files) > 15 else "")
            parts.append(f'<div class="has-tip" data-tip="{_h(tip)}"><span class="extra-q">?</span>'
                         f'{len(files)} file{"s" if len(files) != 1 else ""} with no recognisable '
                         f'episode number</div>')
        if not r.missing:
            parts.insert(0, '<div class="all-ok">✓ All aired episodes present</div>')
        return "\n".join(parts)

    overrides_path = _h(meta.get("overrides_path") or str(DEFAULT_OVERRIDES_PATH))

    def build_panel(r) -> str:
        # ── Match ────────────────────────────────────────────────────────────
        m = []
        if r.lookup_error:
            m.append(f'<div class="warn">⚠ {_h(r.lookup_error)}</div>')
            m.append(f'<div class="hint">To pin this folder to a show, add a line like '
                     f'<code>{_h(json.dumps(r.show_name, ensure_ascii=False))}: "tvmaze:ID"</code> '
                     f'(or <code>"tvdb:ID"</code>, <code>"tmdb:ID"</code>, <code>"episodate:ID"</code>) to <span class="path">{overrides_path}</span></div>')
        else:
            url = r.source_url
            title = _h(r.matched_title or r.show_name)
            link = f'<a href="{_h(url)}" target="_blank" rel="noopener">{title} ↗</a>' if url else title
            conf = f" · confidence {r.match_confidence:.0%}" if r.match_confidence is not None else ""
            m.append(f'<div><strong>{link}</strong> <span class="dim">on {_h(source_label(r.source_used))}{conf}</span></div>')
            if r.match_note:
                m.append(f'<div class="dim">{_h(r.match_note)}</div>')
            if r.match_flag:
                why = ("Low confidence — check this is the right show."
                       if r.match_flag == "low" else
                       "Ambiguous — another show scored almost as well.")
                m.append(f'<div class="warn">⚠ {why}</div>')
            rows = []
            if r.source_id:
                rows.append(f'<li><span class="dim">this match</span> <code>{_h(_pin_line(r.show_name, r.source_used, r.source_id))}</code></li>')
            for a in r.alternatives[:4]:
                if not a.get("id"):
                    continue
                yr = f" ({a['year']})" if a.get("year") and not a["title"].rstrip().endswith(")") else ""
                aurl = a.get("url", "")
                rows.append(
                    f'<li><a href="{_h(aurl)}" target="_blank" rel="noopener">{_h(a["title"])}{yr} ↗</a> '
                    f'<span class="dim">{_h(source_label(a["source"]))} · {a["score"]:.0%}</span> '
                    f'<code>{_h(_pin_line(r.show_name, a["source"], a["id"]))}</code></li>'
                )
            if rows:
                m.append('<h4 style="margin-top:.6rem">Pin this folder to a show</h4>')
                m.append(f'<ul>{"".join(rows)}</ul>')
                m.append(f'<div class="hint">Copy a line into <span class="path">{overrides_path}</span> '
                         f'to fix the match permanently. Nothing in the library needs renaming.</div>')

        # ── On disk ──────────────────────────────────────────────────────────
        d = []
        by_season: Dict[int, set] = {}
        dirs: Dict[int, set] = {}
        for e in r.local_episodes:
            if e.episode == 0:
                continue
            by_season.setdefault(e.season, set()).add(e.episode)
            dirs.setdefault(e.season, set()).add(str(e.path.parent))
        aired_by_season: Dict[int, int] = {}
        for e in r.remote_episodes:
            if e.airdate and e.airdate <= today:
                aired_by_season[e.season] = aired_by_season.get(e.season, 0) + 1
        for s in sorted(by_season):
            tags = " ".join(f'<span class="ep-tag have">{ep_label(a, b)}</span>' for a, b in _runs(by_season[s]))
            of = f' <span class="dim">of {aired_by_season[s]}</span>' if s in aired_by_season else ""
            paths = "<br>".join(_h(p) for p in sorted(dirs[s]))
            d.append(f'<li><strong>S{s:02d}</strong> · {len(by_season[s])}{of} · {tags}'
                     f'<div class="path">{paths}</div></li>')
        files = unparsed(r)
        if files:
            shown = "<br>".join(_h(f) for f in files[:20])
            more = f"<br>… and {len(files) - 20} more" if len(files) > 20 else ""
            d.append(f'<li><span class="warn">No recognisable episode number:</span>'
                     f'<div class="path">{shown}{more}</div></li>')
        disk = f'<ul>{"".join(d)}</ul>' if d else '<span class="dim">No recognised episodes.</span>'

        return (f'<div class="panel"><div><h4>Match</h4>{"".join(m)}</div>'
                f'<div><h4>On disk</h4>{disk}</div></div>')

    show_rows = []
    for r in sorted(reports, key=lambda x: x.show_name.lower()):
        if missing_only and r.ok:
            continue

        row_class = "row-ok" if r.ok else "row-issue"
        aired = aired_count(r)
        have = max(0, aired - len(r.missing))
        if r.lookup_error or not aired:
            eps_html = f'<div class="eps-num">{len(known(r))} <span class="dim">/ ?</span></div>'
            pct_sort = -1.0
        else:
            pct = have / aired
            pct_sort = pct
            cls = "bar" if pct >= 0.999 else "bar partial"
            eps_html = (f'<div class="eps-num">{have} <span class="dim">/ {aired}</span></div>'
                        f'<div class="{cls}"><span style="width:{pct * 100:.1f}%"></span></div>')

        notes = ""
        if r.matched_title and r.matched_title != r.show_name:
            notes += f'<span class="matched-title dim">→ {_h(r.matched_title)}</span>'
        if r.match_flag:
            why = "low-confidence match" if r.match_flag == "low" else "ambiguous match"
            notes += (f'<span class="matched-title warn">⚠ {why} '
                      f'({r.match_confidence:.0%}) — expand to review</span>')
        elif r.match_note.startswith("pinned"):
            notes += '<span class="matched-title dim">📌 pinned</span>'

        status_html = ""
        if r.status:
            status_html = badge(r.status, STATUS_COLORS.get(r.status, "#6b7280"),
                                STATUS_DESC.get(r.status, f"Show status: {r.status}"))
        source_html = badge("—" if r.source_used == "none" else r.source_used.upper(), SOURCE_COLORS.get(r.source_used, "#6b7280"),
                            SOURCE_DESC.get(r.source_used, r.source_used), "source-badge")
        search_key = _h(f"{r.show_name} {r.matched_title or ''}".lower())

        show_rows.append(f"""
        <tr class="main {row_class}" data-issue="{0 if r.ok else 1}" data-flag="{1 if r.match_flag or r.lookup_error else 0}" data-search="{search_key}">
          <td class="ok-col" data-sort="{1 if r.ok else 0}">{'✓' if r.ok else '✗'}</td>
          <td class="show-name" data-sort="{_h(r.show_name)}">
            <button class="show-btn" type="button" aria-expanded="false"><span class="chev">▸</span><span>{_h(r.show_name)}</span></button>
            {notes}
          </td>
          <td data-sort="{_h(r.status or '')}">{status_html}</td>
          <td data-sort="{_h(r.source_used)}">{source_html}</td>
          <td class="eps-col" data-sort="{pct_sort:.4f}">{eps_html}</td>
          <td class="num miss-count" data-sort="{len(r.missing)}">{len(r.missing) if r.missing else ''}</td>
          <td class="detail-col">{build_detail(r)}</td>
        </tr>
        <tr class="panel-row" hidden><td></td><td colspan="6">{build_panel(r)}</td></tr>""")

    rows_html = "\n".join(show_rows)
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    meta_bits = [f"Generated {generated}"]
    if meta.get("root"):
        meta_bits.append(f"Library <code>{_h(meta['root'])}</code>")
    if meta.get("source"):
        meta_bits.append(f"Sources: {_h(meta['source'])}")
    if missing_only:
        meta_bits.append("Complete shows omitted (--missing-only)")

    def card(label: str, value, colour: str = "", sub: str = "") -> str:
        style = f' style="color:var(--{colour})"' if colour else ""
        sub_html = f'<div class="sub">{sub}</div>' if sub else ""
        return (f'<div class="card"><div class="label">{label}</div>'
                f'<div class="value"{style}>{value}</div>{sub_html}</div>')

    review_count = flagged_count + unmatched_count
    cards = "\n  ".join([
        card("Shows scanned", len(reports)),
        card("Complete", ok_count, "green"),
        card("With gaps or errors", issue_count, "red" if issue_count else "green"),
        card("Matches to review", review_count, "yellow" if review_count else "green",
             f"{flagged_count} uncertain · {unmatched_count} unmatched" if review_count else ""),
        card("Episodes on disk", f"{total_local:,}", "", f"{overall_pct:.0%} of aired" if total_aired else ""),
        card("Missing episodes", f"{total_missing:,}", "red" if total_missing else "green"),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<title>tv-scanner report</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<div class="tooltip-box" id="tt"></div>
<h1>tv-scanner report</h1>
<div class="meta">{' &nbsp;·&nbsp; '.join(meta_bits)}</div>

<div class="summary">
  {cards}
</div>

<div class="toolbar">
  <input id="search" type="search" placeholder="Filter by show name…" aria-label="Filter by show name">
  <label class="filter-label"><input type="checkbox" id="issues-only"> Gaps or errors only</label>
  <label class="filter-label"><input type="checkbox" id="flagged-only"> Matches to review only</label>
  <button class="tbtn" id="toggle-all" type="button">Expand all</button>
  <span id="row-count"></span>
</div>

<table id="main-table">
  <thead>
    <tr>
      <th class="sortable has-tip" data-tip="Complete (✓) or has gaps / errors (✗)."><span class="arrow"></span></th>
      <th class="sortable has-tip" data-tip="Show folder name. Click a show to expand it: which series it was matched to and why,&#10;other candidates (with ready-to-paste pin lines), and what is on disk per season."><span class="th-label">Show</span> <span class="arrow"></span></th>
      <th class="sortable has-tip" data-tip="Current airing status from the metadata source."><span class="th-label">Status</span> <span class="arrow"></span></th>
      <th class="sortable has-tip" data-tip="Metadata source whose episode list was used."><span class="th-label">Source</span> <span class="arrow"></span></th>
      <th class="sortable num has-tip" data-tip="Aired episodes you have / aired episodes that exist.&#10;Specials and unaired episodes are not counted. Sorts by percentage complete."><span class="th-label">Episodes</span> <span class="arrow"></span></th>
      <th class="sortable num has-tip" data-tip="Number of aired episodes not found on disk."><span class="th-label">Missing</span> <span class="arrow"></span></th>
      <th class="has-tip" data-tip="Which episodes are missing. Hover an episode range for names and air dates."><span class="th-label">Detail</span></th>
    </tr>
  </thead>
  <tbody id="main-tbody">
    {rows_html}
  </tbody>
</table>

<footer>tv-scanner &nbsp;·&nbsp; Episode data from TVmaze, TheTVDB, TMDB and Episodate &nbsp;·&nbsp; Click a show to expand it; hover headers, badges and episode ranges for details</footer>

<script>{_HTML_JS}</script>
</body>
</html>
"""


def format_csv(reports: list) -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "show_name", "matched_title", "tvmaze_id", "status", "source_used",
        "local_episodes", "remote_episodes", "missing_count", "ok", "error",
        "match_confidence", "match_flag", "match_note", "source_id", "source_url",
    ])
    for r in sorted(reports, key=lambda x: x.show_name.lower()):
        writer.writerow([
            r.show_name, r.matched_title or "", r.tvmaze_id or "",
            r.status or "", r.source_used, len(r.local_episodes),
            len(r.remote_episodes), len(r.missing), r.ok, r.lookup_error or "",
            f"{r.match_confidence:.2f}" if r.match_confidence is not None else "",
            r.match_flag, r.match_note, r.source_id or "", r.source_url,
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def _require_outside_library(path: Path, root: Path, what: str) -> None:
    """The media library is strictly read-only for this tool."""
    if _inside(path, root):
        print(
            f"ERROR: {what} ({path}) is inside the media library ({root}).\n"
            f"       tv-scanner never writes to or keeps its files in the library — "
            f"choose a location outside it.",
            file=sys.stderr,
        )
        sys.exit(1)


def main():
    global USE_COLOR

    parser = argparse.ArgumentParser(
        description="Compare a local TV library against TVmaze and/or TheTVDB episode data."
    )
    parser.add_argument("root", help="Root directory of the TV library")
    parser.add_argument(
        "--output", choices=["text", "json", "csv", "html"], default="text",
        help="Output format (default: text)"
    )
    parser.add_argument(
        "--html", metavar="FILE", nargs="?", const="",
        help="Also write a self-contained HTML report. "
             "Omit FILE to write report.html in the current directory."
    )
    parser.add_argument(
        "--missing-only", action="store_true",
        help="Only show shows with missing episodes or errors"
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Parallel API workers (default: 4)"
    )
    parser.add_argument(
        "--outfile", default=None,
        help="Write output to this file instead of stdout"
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable color output even if the terminal supports it"
    )
    parser.add_argument(
        "--source", default="tvmaze", metavar="LIST",
        help="Databases to query, comma-separated: tvmaze, episodate (no key needed), "
             "thetvdb, tmdb (need a key), 'both' (tvmaze,thetvdb) or 'all' (every source "
             "usable with the keys supplied). With several, each show uses whichever "
             "database fits the local files best. Default: tvmaze."
    )
    parser.add_argument(
        "--thetvdb-apikey", default=None, metavar="PATH",
        help="Path to file containing TheTVDB API key (required for source thetvdb)"
    )
    parser.add_argument(
        "--tmdb-apikey", default=None, metavar="PATH",
        help="Path to file containing a TMDB API key or API Read Access Token "
             "(required for source tmdb)"
    )
    parser.add_argument(
        "--prefer-animation", action="store_true",
        help="Favour animated series when choosing between candidates "
             "(use for a cartoon-only library)"
    )
    parser.add_argument(
        "--overrides", default=None, metavar="FILE",
        help=f"JSON file pinning folders to show IDs, e.g. "
             f'{{"House of Cards": "tvdb:79861"}}. '
             f"Default: {DEFAULT_OVERRIDES_PATH}, if present. Must be outside the library."
    )
    parser.add_argument(
        "--cache-dir", default=None, metavar="DIR",
        help="Where API responses are cached (default: ~/.cache/tv-scanner)"
    )
    parser.add_argument(
        "--cache-ttl", type=float, default=24.0, metavar="HOURS",
        help="How long cached API responses stay fresh (default: 24)"
    )
    parser.add_argument(
        "--refresh", action="store_true",
        help="Ignore cached API responses for this run (and replace them)"
    )
    parser.add_argument(
        "--no-cache", action="store_true",
        help="Do not read or write the API response cache"
    )
    args = parser.parse_args()

    global CACHE_DIR, CACHE_TTL, CACHE_REFRESH, OVERRIDES
    if not args.no_cache:
        CACHE_DIR = Path(args.cache_dir).expanduser() if args.cache_dir else Path.home() / ".cache" / "tv-scanner"
        CACHE_TTL = args.cache_ttl * 3600
        CACHE_REFRESH = args.refresh

    global PREFER_ANIMATION
    PREFER_ANIMATION = args.prefer_animation

    try:
        sources = parse_sources(args.source)
    except ValueError as e:
        parser.error(f"--source: {e}")
    use_all = "all" in [p.strip().lower() for p in args.source.split(",")]

    # Keyed sources: a key alone also enables that database's ID overrides
    key_files = {"thetvdb": args.thetvdb_apikey, "tmdb": args.tmdb_apikey}
    inits = {"thetvdb": thetvdb_init, "tmdb": tmdb_init}
    for name, key_file in key_files.items():
        label, flag = SOURCES[name]["label"], SOURCES[name]["key_flag"]
        if not key_file:
            if name in sources and use_all:
                sources.remove(name)          # "all" quietly skips sources without a key
                print(f"Skipping {label}: no {flag} given", file=sys.stderr)
            elif name in sources:
                print(f"ERROR: {flag} PATH is required for source {name}", file=sys.stderr)
                sys.exit(1)
            continue
        try:
            api_key = Path(key_file).expanduser().read_text().strip()
        except Exception as e:
            print(f"ERROR: Could not read {label} API key from {key_file}: {e}", file=sys.stderr)
            sys.exit(1)
        try:
            inits[name](api_key)
            print(f"{label} authenticated.", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: {label} authentication failed: {e}", file=sys.stderr)
            sys.exit(1)
    print(f"Sources: {', '.join(source_label(s) for s in sources)}"
          + ("  (preferring animation)" if PREFER_ANIMATION else ""), file=sys.stderr)

    # Set color flag — only for text output to stdout
    if args.output == "text" and not args.outfile and not args.no_color:
        USE_COLOR = _detect_color_support()

    # HTML output requires --outfile (warn if going to stdout)
    if args.output == "html" and not args.outfile:
        print("Warning: outputting raw HTML to stdout. Consider using --outfile report.html", file=sys.stderr)

    root = Path(args.root).expanduser().resolve()

    # The library is read-only: every file this tool writes or owns must live elsewhere.
    html_path = None
    if args.html is not None:
        html_path = Path(args.html).expanduser() if args.html else Path.cwd() / "report.html"
        _require_outside_library(html_path, root, "--html report")
    if args.outfile:
        _require_outside_library(Path(args.outfile).expanduser(), root, "--outfile")
    if CACHE_DIR is not None:
        _require_outside_library(CACHE_DIR, root, "cache directory")
    overrides_path = Path(args.overrides).expanduser() if args.overrides else DEFAULT_OVERRIDES_PATH
    _require_outside_library(overrides_path, root, "overrides file")
    print(f"Scanning library: {root}", file=sys.stderr)

    if args.overrides or overrides_path.is_file():
        try:
            OVERRIDES = load_overrides(overrides_path)
        except Exception as e:
            print(f"ERROR: Could not read overrides from {overrides_path}: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"Loaded {len(OVERRIDES)} override(s) from {overrides_path}", file=sys.stderr)

    library = scan_local_library(root)
    unused = [name for name in OVERRIDES if name not in library]
    for name in unused:
        print(f"Warning: override for {name!r} matches no show folder", file=sys.stderr)
    print(f"Found {len(library)} show directories", file=sys.stderr)

    reports: List[ShowReport] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_show, name, eps, sources): name
            for name, eps in library.items()
        }
        done = 0
        for future in as_completed(futures):
            done += 1
            show_name = futures[future]
            try:
                report = future.result()
            except Exception as exc:
                print(f"  [{done}/{len(library)}] ERROR {show_name}: {exc}", file=sys.stderr)
                continue
            status_icon = "✓" if report.ok else "✗"
            src = f"[{report.source_used}]" if len(sources) > 1 and report.source_used != "none" else ""
            print(
                f"  [{done}/{len(library)}] {status_icon} {show_name} {src}"
                + (f" — {len(report.missing)} missing" if report.missing else "")
                + (f" — {report.lookup_error}" if report.lookup_error else "")
                + (f" — {report.match_flag} match → {report.matched_title}" if report.match_flag else ""),
                file=sys.stderr,
            )
            reports.append(report)

    if CACHE_DIR is not None:
        print(
            f"\nAPI requests: {_cache_stats['miss']} fetched, {_cache_stats['hit']} from cache ({CACHE_DIR})",
            file=sys.stderr,
        )
    print(f"\nGenerating report…", file=sys.stderr)
    html_meta = {"root": str(root), "source": ", ".join(source_label(s) for s in sources), "overrides_path": str(overrides_path)}

    if args.output == "text":
        output = format_text(reports, args.missing_only)
    elif args.output == "json":
        output = format_json(reports)
    elif args.output == "html":
        output = format_html(reports, args.missing_only, html_meta)
    else:
        output = format_csv(reports)

    if args.outfile:
        Path(args.outfile).expanduser().write_text(output, encoding="utf-8")
        print(f"Report written to {args.outfile}", file=sys.stderr)
    else:
        print(output)

    # --html: write HTML report in addition to (or instead of) the primary output
    if html_path is not None:
        html_path.write_text(format_html(reports, args.missing_only, html_meta), encoding="utf-8")
        print(f"HTML report written to {html_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
