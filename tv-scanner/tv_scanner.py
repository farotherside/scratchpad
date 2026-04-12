#!/usr/bin/env python3
"""
tv_scanner.py — Compare a local TV library against TVmaze and/or TheTVDB episode data.

Usage:
    python tv_scanner.py /path/to/tv/root [options]

Options:
    --output {text,json,csv,html}  Output format (default: text)
    --missing-only                 Only show shows/episodes that have gaps
    --no-ended                     Skip shows whose status is Ended / To Be Determined
    --workers N                    Parallel API workers (default: 4)
    --no-color                     Disable color output
    --source {tvmaze,thetvdb}      API source to use (default: tvmaze)
    --thetvdb-apikey PATH          Path to file containing TheTVDB API key

When --source thetvdb is used, both TVmaze AND TheTVDB are queried for each show.
The episode list that most closely matches the local filesystem is used.

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
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TVMAZE_BASE = "https://api.tvmaze.com"
THETVDB_BASE = "https://api4.thetvdb.com/v4"
REQUEST_TIMEOUT = 15          # seconds per HTTP request
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0           # seconds
RATE_LIMIT_DELAY = 0.2        # polite delay between requests (TVmaze is free/public)
THETVDB_RATE_LIMIT_DELAY = 0.5

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
    source_used: str = "tvmaze"   # "tvmaze" or "thetvdb"

    @property
    def ok(self) -> bool:
        return not self.missing and not self.lookup_error


# ---------------------------------------------------------------------------
# Generic HTTP helper
# ---------------------------------------------------------------------------
def _request(url: str, params: dict = None, headers: dict = None, rate_delay: float = RATE_LIMIT_DELAY) -> dict | list:
    """GET with retry + polite rate-limit."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", RETRY_BACKOFF * (attempt + 1)))
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            time.sleep(rate_delay)
            return resp.json()
        except requests.RequestException as exc:
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
_EP_PATTERNS = [
    re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE),
    re.compile(r"(\d+)[xX](\d+)"),
    re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)"),
]


def parse_episode_from_filename(filename: str) -> Optional[tuple]:
    stem = Path(filename).stem
    for pat in _EP_PATTERNS:
        m = pat.search(stem)
        if m:
            s, e = int(m.group(1)), int(m.group(2))
            if 0 < s < 100 and 0 < e < 300:
                return s, e
    return None


# ---------------------------------------------------------------------------
# Local library scan
# ---------------------------------------------------------------------------
def scan_local_library(root: Path) -> dict:
    library: dict = {}

    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    for show_dir in sorted(root.iterdir()):
        if not show_dir.is_dir() or show_dir.name.startswith("."):
            continue

        episodes = []

        for season_dir in sorted(show_dir.iterdir()):
            if not season_dir.is_dir():
                continue
            season_num = parse_season_number(season_dir.name)
            if season_num is None:
                continue

            for f in sorted(season_dir.iterdir()):
                if f.suffix.lower() not in VIDEO_EXTS:
                    continue
                parsed = parse_episode_from_filename(f.name)
                if parsed:
                    ep_s, ep_e = parsed
                    episodes.append(LocalEpisode(season=ep_s, episode=ep_e, path=f))
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


def _tvmaze_aired_count(tvmaze_id: int) -> int:
    today = time.strftime("%Y-%m-%d")
    eps = tvmaze_fetch_episodes(tvmaze_id)
    return sum(1 for e in eps if e.airdate and e.airdate <= today)


def tvmaze_search_show(show_name: str, local_ep_count: int = 0) -> Optional[dict]:
    results = _request(f"{TVMAZE_BASE}/search/shows", params={"q": show_name})
    if not results:
        return None

    candidates = [
        r["show"] for r in results
        if r["show"].get("status", "") != "In Development"
    ]
    if not candidates:
        return None
    if len(candidates) == 1 or local_ep_count == 0:
        return candidates[0]

    top = candidates[:5]
    best_show = top[0]
    best_diff = float("inf")
    for show in top:
        try:
            remote_count = _tvmaze_aired_count(show["id"])
            diff = abs(remote_count - local_ep_count)
            if diff < best_diff:
                best_diff = diff
                best_show = show
        except Exception:
            pass
    return best_show


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


def thetvdb_search_show(show_name: str) -> Optional[dict]:
    """Return best TheTVDB series match or None."""
    try:
        data = _request(
            f"{THETVDB_BASE}/search",
            params={"query": show_name, "type": "series"},
            headers=_thetvdb_headers(),
            rate_delay=THETVDB_RATE_LIMIT_DELAY,
        )
        results = data.get("data", [])
        if not results:
            return None
        # Return the top result
        return results[0]
    except Exception:
        return None


def thetvdb_fetch_episodes(series_id: int) -> list:
    """Fetch all regular episodes for a TheTVDB series ID."""
    if series_id in _thetvdb_episode_cache:
        return _thetvdb_episode_cache[series_id]

    episodes = []
    page = 0
    while True:
        try:
            data = _request(
                f"{THETVDB_BASE}/series/{series_id}/episodes/official",
                params={"page": page, "season": 0},
                headers=_thetvdb_headers(),
                rate_delay=THETVDB_RATE_LIMIT_DELAY,
            )
        except Exception:
            break

        eps = data.get("data", {}).get("episodes", [])
        if not eps:
            break

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

        # TheTVDB paginates; check if there are more pages
        links = data.get("links", {})
        if not links.get("next"):
            break
        page += 1

    _thetvdb_episode_cache[series_id] = episodes
    return episodes


# ---------------------------------------------------------------------------
# Match scoring — how well does a remote episode list match local files?
# ---------------------------------------------------------------------------
def _match_score(local: list, remote: list) -> float:
    """
    Score how well a remote episode list matches local files.
    Returns a score where HIGHER is BETTER.
    Based on: fraction of local episodes found in remote list.
    """
    if not remote or not local:
        return 0.0

    today = time.strftime("%Y-%m-%d")
    remote_set = {
        (ep.season, ep.episode) for ep in remote
        if ep.airdate and ep.airdate <= today
    }
    local_known = [(ep.season, ep.episode) for ep in local if ep.episode != 0]
    if not local_known:
        return 0.0

    matches = sum(1 for ep in local_known if ep in remote_set)
    return matches / len(local_known)


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
def process_show(show_name: str, local_eps: list, source: str = "tvmaze") -> ShowReport:
    local_ep_count = sum(1 for ep in local_eps if ep.episode != 0)

    try:
        # --- Always fetch from TVmaze ---
        tvmaze_show = tvmaze_search_show(show_name, local_ep_count=local_ep_count)
        tvmaze_eps = []
        if tvmaze_show:
            try:
                tvmaze_eps = tvmaze_fetch_episodes(tvmaze_show["id"])
            except Exception:
                tvmaze_eps = []

        # --- Optionally fetch from TheTVDB ---
        thetvdb_show = None
        thetvdb_eps = []
        if source == "thetvdb" and _thetvdb_token:
            thetvdb_show = thetvdb_search_show(show_name)
            if thetvdb_show:
                try:
                    series_id = thetvdb_show.get("tvdb_id") or thetvdb_show.get("id")
                    if series_id:
                        thetvdb_eps = thetvdb_fetch_episodes(int(series_id))
                except Exception:
                    thetvdb_eps = []

        # --- Pick best source ---
        if source == "thetvdb" and thetvdb_eps:
            tvmaze_score = _match_score(local_eps, tvmaze_eps)
            thetvdb_score = _match_score(local_eps, thetvdb_eps)

            if thetvdb_score >= tvmaze_score:
                # TheTVDB wins (or tied — prefer thetvdb since that was requested)
                chosen_show = thetvdb_show
                chosen_eps = thetvdb_eps
                chosen_source = "thetvdb"
                matched_title = thetvdb_show.get("name") or thetvdb_show.get("translations", {}).get("eng", show_name)
                show_status = thetvdb_show.get("status", {})
                if isinstance(show_status, dict):
                    show_status = show_status.get("name", "Unknown")
                tvmaze_id = tvmaze_show["id"] if tvmaze_show else None
            else:
                # TVmaze is a better fit
                chosen_show = tvmaze_show
                chosen_eps = tvmaze_eps
                chosen_source = "tvmaze"
                matched_title = tvmaze_show["name"] if tvmaze_show else None
                show_status = tvmaze_show.get("status", "Unknown") if tvmaze_show else "Unknown"
                tvmaze_id = tvmaze_show["id"] if tvmaze_show else None
        else:
            # Pure TVmaze mode
            if not tvmaze_show:
                return ShowReport(
                    show_name=show_name,
                    matched_title=None,
                    tvmaze_id=None,
                    status=None,
                    local_seasons=set(),
                    local_episodes=local_eps,
                    remote_episodes=[],
                    missing=[],
                    extra=[],
                    lookup_error="No TVmaze match found",
                    source_used="tvmaze",
                )
            chosen_show = tvmaze_show
            chosen_eps = tvmaze_eps
            chosen_source = "tvmaze"
            matched_title = tvmaze_show["name"]
            show_status = tvmaze_show.get("status", "Unknown")
            tvmaze_id = tvmaze_show["id"]

        if not chosen_eps:
            return ShowReport(
                show_name=show_name,
                matched_title=matched_title if 'matched_title' in dir() else None,
                tvmaze_id=tvmaze_id if 'tvmaze_id' in dir() else None,
                status=None,
                local_seasons=set(),
                local_episodes=local_eps,
                remote_episodes=[],
                missing=[],
                extra=[],
                lookup_error=f"No episodes found via {source}",
                source_used=chosen_source if 'chosen_source' in dir() else source,
            )

        missing, extra = compare(local_eps, chosen_eps)

        return ShowReport(
            show_name=show_name,
            matched_title=matched_title,
            tvmaze_id=tvmaze_id,
            status=show_status,
            local_seasons={ep.season for ep in local_eps},
            local_episodes=local_eps,
            remote_episodes=chosen_eps,
            missing=missing,
            extra=extra,
            lookup_error=None,
            source_used=chosen_source,
        )

    except Exception as exc:
        return ShowReport(
            show_name=show_name,
            matched_title=None,
            tvmaze_id=None,
            status=None,
            local_seasons=set(),
            local_episodes=local_eps,
            remote_episodes=[],
            missing=[],
            extra=[],
            lookup_error=str(exc),
            source_used=source,
        )


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

        local_count = len(report.local_episodes)
        remote_count = len(report.remote_episodes)
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


def format_html(reports: list, missing_only: bool) -> str:
    from datetime import datetime

    ok_count = sum(1 for r in reports if r.ok)
    issue_count = len(reports) - ok_count
    total_local = sum(len(r.local_episodes) for r in reports)
    total_missing = sum(len(r.missing) for r in reports)

    # Status colour map
    STATUS_COLORS = {
        "Ended":              "#64748b",
        "Continuing":         "#22c55e",
        "To Be Determined":   "#f59e0b",
        "In Development":     "#6366f1",
    }
    SOURCE_COLORS = {
        "tvmaze":  "#6366f1",
        "thetvdb": "#f59e0b",
    }

    def status_badge(status: str) -> str:
        colour = STATUS_COLORS.get(status, "#6b7280")
        return (f'<span class="badge" style="background:{colour}22;'
                f'color:{colour};border:1px solid {colour}44">{_h(status)}</span>')

    def source_badge(source: str) -> str:
        colour = SOURCE_COLORS.get(source, "#6b7280")
        return (f'<span class="badge source-badge" style="background:{colour}22;'
                f'color:{colour};border:1px solid {colour}44">{_h(source.upper())}</span>')

    def ep_tag(ep) -> str:
        return f'<span class="ep-tag">S{ep.season:02d}E{ep.episode:02d}</span>'

    show_rows = []
    for r in sorted(reports, key=lambda x: x.show_name.lower()):
        if missing_only and r.ok:
            continue

        ok_icon = '✓' if r.ok else '✗'
        row_class = 'row-ok' if r.ok else 'row-issue'

        if r.lookup_error:
            detail_html = f'<span class="warn">⚠ {_h(r.lookup_error)}</span>'
        else:
            detail_parts = []
            if r.missing:
                by_season: dict = {}
                for ep in r.missing:
                    by_season.setdefault(ep.season, []).append(ep)
                local_seasons_with_eps = {ep.season for ep in r.local_episodes if ep.episode != 0}
                today = time.strftime("%Y-%m-%d")
                for s in sorted(by_season):
                    eps_in_season = by_season[s]
                    if s not in local_seasons_with_eps:
                        count_str = f"{len(eps_in_season)} ep{'s' if len(eps_in_season)!=1 else ''}"
                        detail_parts.append(
                            f'<div class="missing-line"><span class="miss-x">✗</span> '
                            f'<strong>S{s:02d}</strong> entirely missing '
                            f'<span class="dim">({count_str})</span></div>'
                        )
                    else:
                        tags = " ".join(ep_tag(e) for e in sorted(eps_in_season, key=lambda x: x.episode))
                        detail_parts.append(
                            f'<div class="missing-line"><span class="miss-x">✗</span> '
                            f'Missing <strong>S{s:02d}</strong>: {tags}</div>'
                        )
            if r.extra:
                tags = " ".join(ep_tag(e) for e in sorted(r.extra, key=lambda x: (x.season, x.episode)))
                src = r.source_used.upper()
                detail_parts.append(
                    f'<div class="extra-line"><span class="extra-q">?</span> '
                    f'Extra (not in {src}): {tags}</div>'
                )
            if not detail_parts:
                detail_parts.append('<span class="all-ok">✓ All aired episodes present</span>')
            detail_html = "\n".join(detail_parts)

        matched_note = ""
        if r.matched_title and r.matched_title != r.show_name:
            matched_note = f'<span class="matched-title dim">→ {_h(r.matched_title)}</span>'

        show_rows.append(f"""
        <tr class="{row_class}">
          <td class="ok-col">{ok_icon}</td>
          <td class="show-name">
            {_h(r.show_name)}
            {matched_note}
          </td>
          <td>{status_badge(r.status or 'Unknown') if r.status else ''}</td>
          <td>{source_badge(r.source_used)}</td>
          <td class="num">{len(r.local_episodes)}</td>
          <td class="num">{len(r.remote_episodes)}</td>
          <td class="num miss-count">{len(r.missing) if r.missing else ''}</td>
          <td class="detail-col">{detail_html}</td>
        </tr>""")

    rows_html = "\n".join(show_rows)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tv-scanner report</title>
<style>
  :root {{
    --bg:      #0f1117;
    --surface: #1a1d27;
    --border:  #2a2d3a;
    --text:    #e2e8f0;
    --dim:     #64748b;
    --accent:  #6366f1;
    --green:   #22c55e;
    --red:     #ef4444;
    --yellow:  #f59e0b;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 13px;
    padding: 2rem;
  }}
  h1 {{ font-size: 1.4rem; font-weight: 700; margin-bottom: .25rem; color: #f1f5f9; }}
  .meta {{ color: var(--dim); font-size: .8rem; margin-bottom: 1.5rem; }}
  .summary {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: .75rem;
    margin-bottom: 1.5rem;
  }}
  .card {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: .75rem 1rem;
  }}
  .card .label {{ font-size: .7rem; text-transform: uppercase;
                  letter-spacing: .05em; color: var(--dim); }}
  .card .value {{ font-size: 1.2rem; font-weight: 700; margin-top: .2rem; }}
  .toolbar {{ display: flex; align-items: center; gap: 1rem; margin-bottom: .75rem; flex-wrap: wrap; }}
  input#search {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--text);
    padding: .4rem .8rem;
    font-size: .85rem;
    outline: none;
    width: 260px;
  }}
  input#search:focus {{ border-color: var(--accent); }}
  label.filter-label {{
    color: var(--dim);
    font-size: .8rem;
    display: flex;
    align-items: center;
    gap: .35rem;
    cursor: pointer;
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }}
  thead th {{
    background: var(--surface);
    color: var(--dim);
    font-weight: 600;
    text-transform: uppercase;
    font-size: .68rem;
    letter-spacing: .04em;
    padding: .55rem .6rem;
    border-bottom: 1px solid var(--border);
    text-align: left;
    position: sticky;
    top: 0;
    cursor: pointer;
    user-select: none;
  }}
  thead th:hover {{ color: var(--text); }}
  thead th.sorted {{ color: var(--accent); }}
  .num {{ text-align: right; }}
  tbody tr {{
    border-bottom: 1px solid var(--border);
    transition: background .1s;
  }}
  tbody tr:hover {{ background: var(--surface); }}
  td {{ padding: .5rem .6rem; vertical-align: top; }}
  .ok-col {{ width: 24px; text-align: center; font-weight: 700; }}
  .row-ok  .ok-col {{ color: var(--green); }}
  .row-issue .ok-col {{ color: var(--red); }}
  .show-name {{ font-weight: 600; color: #cbd5e1; min-width: 180px; }}
  .matched-title {{ display: block; font-size: .75rem; font-weight: 400; margin-top: .15rem; }}
  .dim {{ color: var(--dim); }}
  .badge {{
    display: inline-block;
    padding: .15rem .5rem;
    border-radius: 999px;
    font-size: .7rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .04em;
    white-space: nowrap;
  }}
  .source-badge {{ font-size: .65rem; }}
  .miss-count {{ color: var(--red); font-weight: 700; }}
  .detail-col {{ max-width: 600px; }}
  .missing-line, .extra-line {{ margin: .15rem 0; }}
  .miss-x {{ color: var(--red); font-weight: 700; margin-right: .3rem; }}
  .extra-q {{ color: var(--yellow); font-weight: 700; margin-right: .3rem; }}
  .warn {{ color: var(--yellow); }}
  .all-ok {{ color: var(--green); }}
  .ep-tag {{
    display: inline-block;
    background: var(--border);
    border-radius: 4px;
    padding: .1rem .35rem;
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: .72rem;
    margin: .1rem .1rem;
  }}
  .row-issue .ep-tag {{ background: #ef444422; color: #fca5a5; }}
  footer {{ margin-top: 2rem; color: var(--dim); font-size: .75rem; }}
</style>
</head>
<body>
<h1>⚙ tv-scanner report</h1>
<div class="meta">
  Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
</div>

<div class="summary">
  <div class="card"><div class="label">Shows scanned</div><div class="value">{len(reports)}</div></div>
  <div class="card"><div class="label">Complete</div><div class="value" style="color:var(--green)">{ok_count}</div></div>
  <div class="card"><div class="label">Issues</div><div class="value" style="color:{'var(--red)' if issue_count else 'var(--green)'}">{issue_count}</div></div>
  <div class="card"><div class="label">Local episodes</div><div class="value">{total_local}</div></div>
  <div class="card"><div class="label">Missing episodes</div><div class="value" style="color:{'var(--red)' if total_missing else 'var(--green)'}">{total_missing}</div></div>
</div>

<div class="toolbar">
  <input id="search" type="text" placeholder="Filter by show name…" oninput="filterTable(this.value)">
  <label class="filter-label">
    <input type="checkbox" id="issues-only" onchange="toggleIssues(this.checked)">
    Issues only
  </label>
</div>

<table id="main-table">
  <thead>
    <tr>
      <th onclick="sortTable(0)"></th>
      <th onclick="sortTable(1)">Show</th>
      <th onclick="sortTable(2)">Status</th>
      <th onclick="sortTable(3)">Source</th>
      <th class="num sorted" onclick="sortTable(4)">Local ↕</th>
      <th class="num" onclick="sortTable(5)">Remote ↕</th>
      <th class="num" onclick="sortTable(6)">Missing ↕</th>
      <th>Detail</th>
    </tr>
  </thead>
  <tbody id="main-tbody">
    {rows_html}
  </tbody>
</table>

<footer>tv-scanner &nbsp;·&nbsp; Episode data from TVmaze / TheTVDB</footer>

<script>
  let sortCol = 4, sortAsc = true;
  const numCols = new Set([4, 5, 6]);

  function cellVal(row, col) {{
    return row.cells[col]?.innerText.trim() ?? '';
  }}

  function sortTable(col) {{
    if (sortCol === col) sortAsc = !sortAsc;
    else {{ sortCol = col; sortAsc = true; }}
    const tbody = document.getElementById('main-tbody');
    const rows = Array.from(tbody.rows).filter(r => r.style.display !== 'none' || true);
    rows.sort((a, b) => {{
      let av = cellVal(a, col), bv = cellVal(b, col);
      if (numCols.has(col)) {{
        av = parseInt(av) || 0;
        bv = parseInt(bv) || 0;
        return sortAsc ? av - bv : bv - av;
      }}
      return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
    }});
    rows.forEach(r => tbody.appendChild(r));
    document.querySelectorAll('thead th').forEach((th, i) => {{
      th.classList.toggle('sorted', i === col);
      if (i === col) th.textContent = th.textContent.replace(/ [↑↓]$/, '') + (sortAsc ? ' ↑' : ' ↓');
    }});
  }}

  function filterTable(query) {{
    const q = query.toLowerCase();
    const issuesOnly = document.getElementById('issues-only').checked;
    Array.from(document.getElementById('main-tbody').rows).forEach(row => {{
      const name = cellVal(row, 1).toLowerCase();
      const isIssue = row.classList.contains('row-issue');
      row.style.display = (name.includes(q) && (!issuesOnly || isIssue)) ? '' : 'none';
    }});
  }}

  function toggleIssues(on) {{
    const q = document.getElementById('search').value.toLowerCase();
    Array.from(document.getElementById('main-tbody').rows).forEach(row => {{
      const name = cellVal(row, 1).toLowerCase();
      const isIssue = row.classList.contains('row-issue');
      row.style.display = (name.includes(q) && (!on || isIssue)) ? '' : 'none';
    }});
  }}
</script>
</body>
</html>
"""
    return html


def format_csv(reports: list) -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "show_name", "matched_title", "tvmaze_id", "status", "source_used",
        "local_episodes", "remote_episodes", "missing_count", "ok", "error",
    ])
    for r in sorted(reports, key=lambda x: x.show_name.lower()):
        writer.writerow([
            r.show_name, r.matched_title or "", r.tvmaze_id or "",
            r.status or "", r.source_used, len(r.local_episodes),
            len(r.remote_episodes), len(r.missing), r.ok, r.lookup_error or "",
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
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
        "--source", choices=["tvmaze", "thetvdb"], default="tvmaze",
        help="API source to use for episode data (default: tvmaze). "
             "When 'thetvdb' is selected, both TVmaze and TheTVDB are queried "
             "and the best-matching source is used per show."
    )
    parser.add_argument(
        "--thetvdb-apikey", default=None, metavar="PATH",
        help="Path to file containing TheTVDB API key (required when --source thetvdb)"
    )
    args = parser.parse_args()

    # Validate TheTVDB key requirement
    if args.source == "thetvdb":
        if not args.thetvdb_apikey:
            print("ERROR: --thetvdb-apikey PATH is required when --source thetvdb", file=sys.stderr)
            sys.exit(1)
        try:
            api_key = Path(args.thetvdb_apikey).read_text().strip()
        except Exception as e:
            print(f"ERROR: Could not read TheTVDB API key from {args.thetvdb_apikey}: {e}", file=sys.stderr)
            sys.exit(1)
        print("Authenticating with TheTVDB…", file=sys.stderr)
        try:
            thetvdb_init(api_key)
            print("TheTVDB authenticated.", file=sys.stderr)
        except Exception as e:
            print(f"ERROR: TheTVDB authentication failed: {e}", file=sys.stderr)
            sys.exit(1)

    # Set color flag — only for text output to stdout
    if args.output == "text" and not args.outfile and not args.no_color:
        USE_COLOR = _detect_color_support()

    # HTML output requires --outfile (warn if going to stdout)
    if args.output == "html" and not args.outfile:
        print("Warning: outputting raw HTML to stdout. Consider using --outfile report.html", file=sys.stderr)

    root = Path(args.root).expanduser().resolve()
    print(f"Scanning library: {root}", file=sys.stderr)

    library = scan_local_library(root)
    print(f"Found {len(library)} show directories", file=sys.stderr)

    reports: list = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_show, name, eps, args.source): name
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
            src = f"[{report.source_used}]" if args.source == "thetvdb" else ""
            print(
                f"  [{done}/{len(library)}] {status_icon} {show_name} {src}"
                + (f" — {len(report.missing)} missing" if report.missing else "")
                + (f" — {report.lookup_error}" if report.lookup_error else ""),
                file=sys.stderr,
            )
            reports.append(report)

    print(f"\nGenerating report…", file=sys.stderr)

    if args.output == "text":
        output = format_text(reports, args.missing_only)
    elif args.output == "json":
        output = format_json(reports)
    elif args.output == "html":
        output = format_html(reports, args.missing_only)
    else:
        output = format_csv(reports)

    if args.outfile:
        Path(args.outfile).write_text(output, encoding="utf-8")
        print(f"Report written to {args.outfile}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
