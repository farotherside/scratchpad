#!/usr/bin/env python3
"""
tv_scanner.py — Compare a local TV library against TVmaze episode data.

Usage:
    python tv_scanner.py /path/to/tv/root [options]

Options:
    --output {text,json,csv}   Output format (default: text)
    --missing-only             Only show shows/episodes that have gaps
    --no-ended                 Skip shows whose status is Ended / To Be Determined
    --workers N                Parallel TVmaze workers (default: 4)
    --no-color                 Disable color output

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
REQUEST_TIMEOUT = 15          # seconds per HTTP request
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.0           # seconds
RATE_LIMIT_DELAY = 0.2        # polite delay between requests (TVmaze is free/public)

# File extensions considered video files
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".vob", ".divx",
}

# ---------------------------------------------------------------------------
# Color support
# ---------------------------------------------------------------------------

def _detect_color_support() -> bool:
    """Return True if the terminal appears to support ANSI color.

    Permissive approach: if stdout is a TTY, assume color is supported
    unless TERM is explicitly set to a known non-color value ('dumb') or
    is completely absent.  COLORTERM and TERM_PROGRAM override everything.
    """
    if not sys.stdout.isatty():
        return False
    # Explicit opt-out: NO_COLOR convention (https://no-color.org)
    if os.environ.get("NO_COLOR") is not None:
        return False
    # Explicit opt-in signals
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm:
        return True
    term_program = os.environ.get("TERM_PROGRAM", "").lower()
    if term_program:
        return True
    term = os.environ.get("TERM", "").lower()
    # Only known non-color values are excluded; everything else (incl. 'ansi') gets color
    no_color_terms = {"", "dumb", "unknown"}
    return term not in no_color_terms


# Module-level flag — set in main() after parsing --no-color
USE_COLOR: bool = False


class _C:
    """ANSI escape codes. Only used when USE_COLOR is True."""
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
    """Wrap text in an ANSI code + reset, if color is enabled."""
    if not USE_COLOR:
        return text
    return f"{code}{text}{_C.RESET}"


def _bold(text: str) -> str:
    return _col(_C.BOLD, text)

def _red(text: str) -> str:
    return _col(_C.BRIGHT_RED, text)

def _green(text: str) -> str:
    return _col(_C.BRIGHT_GREEN, text)

def _yellow(text: str) -> str:
    return _col(_C.BRIGHT_YELLOW, text)

def _cyan(text: str) -> str:
    return _col(_C.BRIGHT_CYAN, text)

def _dim(text: str) -> str:
    return _col(_C.DIM, text)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class LocalEpisode:
    season: int
    episode: int           # first episode number if multi-ep file
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
    status: Optional[str]             # Continuing / Ended / etc.
    local_seasons: set[int]
    local_episodes: list[LocalEpisode]
    remote_episodes: list[RemoteEpisode]
    missing: list[RemoteEpisode]
    extra: list[LocalEpisode]         # local files with no TVmaze match
    lookup_error: Optional[str]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.lookup_error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _request(url: str, params: dict = None) -> dict | list:
    """GET with retry + polite rate-limit."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", RETRY_BACKOFF * (attempt + 1)))
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            time.sleep(RATE_LIMIT_DELAY)
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
        season[\s._-]*(\d+)       # "Season 1", "Season_2"
      | s[\s._-]?(\d+)            # "S01", "s1", "s 2"
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def parse_season_number(folder_name: str) -> Optional[int]:
    m = _SEASON_RE.search(folder_name)
    if m:
        val = m.group(1) or m.group(2)
        return int(val)
    # bare number?
    bare = re.fullmatch(r"\d+", folder_name.strip())
    if bare:
        return int(bare.group())
    return None


# ---------------------------------------------------------------------------
# Episode filename parsing
# ---------------------------------------------------------------------------
# Patterns tried in order — first match wins
_EP_PATTERNS = [
    # S01E02, S01E02E03, s1e2
    re.compile(r"[Ss](\d+)[Ee](\d+)", re.IGNORECASE),
    # 1x02
    re.compile(r"(\d+)[xX](\d+)"),
    # .102. or _102_ (season + 2-digit ep, e.g. 102 = s01e02)
    re.compile(r"(?<!\d)(\d)(\d{2})(?!\d)"),
]


def parse_episode_from_filename(filename: str) -> Optional[tuple[int, int]]:
    """Return (season, episode) from a filename, or None."""
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
def scan_local_library(root: Path) -> dict[str, list[LocalEpisode]]:
    """
    Walk root/ looking for show folders → season subfolders → video files.
    Returns {show_folder_name: [LocalEpisode, …]}.
    """
    library: dict[str, list[LocalEpisode]] = {}

    if not root.is_dir():
        print(f"ERROR: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    for show_dir in sorted(root.iterdir()):
        if not show_dir.is_dir() or show_dir.name.startswith("."):
            continue

        episodes: list[LocalEpisode] = []

        for season_dir in sorted(show_dir.iterdir()):
            if not season_dir.is_dir():
                continue
            season_num = parse_season_number(season_dir.name)
            if season_num is None:
                # Try files directly inside show folder (flat layout)
                continue

            for f in sorted(season_dir.iterdir()):
                if f.suffix.lower() not in VIDEO_EXTS:
                    continue
                parsed = parse_episode_from_filename(f.name)
                if parsed:
                    ep_s, ep_e = parsed
                    episodes.append(LocalEpisode(season=ep_s, episode=ep_e, path=f))
                else:
                    # File inside a known season folder but couldn't parse ep#
                    # Treat it as a placeholder (season known, ep unknown = 0)
                    episodes.append(LocalEpisode(season=season_num, episode=0, path=f))

        if episodes:
            library[show_dir.name] = episodes

    return library


# ---------------------------------------------------------------------------
# TVmaze lookups
# ---------------------------------------------------------------------------

# Simple module-level cache so disambiguation + main fetch don't double-hit
_episode_cache: dict[int, list[RemoteEpisode]] = {}


def fetch_episodes(tvmaze_id: int) -> list[RemoteEpisode]:
    """Fetch full episode list for a show (cached)."""
    if tvmaze_id in _episode_cache:
        return _episode_cache[tvmaze_id]

    data = _request(f"{TVMAZE_BASE}/shows/{tvmaze_id}/episodes")
    episodes = []
    for ep in data:
        if ep.get("type", "regular") != "regular":
            continue  # skip specials (type: "significant_special", etc.)
        s = ep.get("season")
        e = ep.get("number")
        if s and e:
            episodes.append(RemoteEpisode(
                season=s,
                episode=e,
                name=ep.get("name", ""),
                airdate=ep.get("airdate", ""),
            ))

    _episode_cache[tvmaze_id] = episodes
    return episodes


def _aired_episode_count(tvmaze_id: int) -> int:
    """Return the number of aired regular episodes for a candidate show."""
    today = time.strftime("%Y-%m-%d")
    eps = fetch_episodes(tvmaze_id)
    return sum(1 for e in eps if e.airdate and e.airdate <= today)


def search_show(show_name: str, local_ep_count: int = 0) -> Optional[dict]:
    """Return the best TVmaze show match or None.

    When multiple candidates exist:
      - 'In Development' shows are always excluded.
      - Among remaining candidates, the one whose aired episode count is
        closest to *local_ep_count* wins.
    """
    results = _request(f"{TVMAZE_BASE}/search/shows", params={"q": show_name})
    if not results:
        return None

    # Filter out shows that are purely "In Development"
    candidates = [
        r["show"] for r in results
        if r["show"].get("status", "") != "In Development"
    ]

    if not candidates:
        return None

    if len(candidates) == 1 or local_ep_count == 0:
        return candidates[0]

    # Multiple candidates — score by closeness of episode count to local library
    # Only check the top 5 results to limit extra API calls
    top = candidates[:5]
    best_show = top[0]
    best_diff = float("inf")

    for show in top:
        try:
            remote_count = _aired_episode_count(show["id"])
            diff = abs(remote_count - local_ep_count)
            if diff < best_diff:
                best_diff = diff
                best_show = show
        except Exception:
            pass  # If we can't fetch, skip this candidate

    return best_show


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------
def compare(
    local: list[LocalEpisode],
    remote: list[RemoteEpisode],
    only_aired: bool = True,
) -> tuple[list[RemoteEpisode], list[LocalEpisode]]:
    """
    Returns (missing_remote_eps, extra_local_eps).
    `only_aired`: ignore remote episodes that have no airdate yet.
    """
    local_set = {(ep.season, ep.episode) for ep in local if ep.episode != 0}
    today = time.strftime("%Y-%m-%d")

    missing = []
    for rep in remote:
        if only_aired and rep.airdate and rep.airdate > today:
            continue  # hasn't aired yet
        if only_aired and not rep.airdate:
            continue  # no known airdate — skip
        if (rep.season, rep.episode) not in local_set:
            missing.append(rep)

    remote_set = {(ep.season, ep.episode) for ep in remote}
    extra = [ep for ep in local if ep.episode != 0 and (ep.season, ep.episode) not in remote_set]

    return missing, extra


# ---------------------------------------------------------------------------
# Per-show worker
# ---------------------------------------------------------------------------
def process_show(show_name: str, local_eps: list[LocalEpisode]) -> ShowReport:
    local_ep_count = sum(1 for ep in local_eps if ep.episode != 0)

    try:
        show = search_show(show_name, local_ep_count=local_ep_count)
        if not show:
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
            )

        tvmaze_id = show["id"]
        matched_title = show["name"]
        status = show.get("status", "Unknown")
        remote_eps = fetch_episodes(tvmaze_id)
        missing, extra = compare(local_eps, remote_eps)

        return ShowReport(
            show_name=show_name,
            matched_title=matched_title,
            tvmaze_id=tvmaze_id,
            status=status,
            local_seasons={ep.season for ep in local_eps},
            local_episodes=local_eps,
            remote_episodes=remote_eps,
            missing=missing,
            extra=extra,
            lookup_error=None,
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
        )


# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------
def _season_ep(ep) -> str:
    return f"S{ep.season:02d}E{ep.episode:02d}"


def format_text(reports: list[ShowReport], missing_only: bool) -> str:
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

        # Build header
        title_part = _bold(_cyan(f"► {report.show_name}"))
        if report.matched_title and report.matched_title != report.show_name:
            title_part += _dim(f"  [TVmaze: {report.matched_title}]")
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
            f"TVmaze (aired): {remote_count} episodes"
        )

        if not report.missing and not report.extra:
            lines.append(f"    {_green('✓ All aired episodes present')}")
        else:
            if report.missing:
                # Build per-season breakdown
                by_season: dict[int, list[RemoteEpisode]] = {}
                for ep in report.missing:
                    by_season.setdefault(ep.season, []).append(ep)

                # Seasons that have at least one local episode
                local_seasons_with_eps = {
                    ep.season for ep in report.local_episodes if ep.episode != 0
                }

                # Aired remote episodes by season (for "entire season" check)
                today = time.strftime("%Y-%m-%d")
                aired_by_season: dict[int, int] = {}
                for ep in report.remote_episodes:
                    if ep.airdate and ep.airdate <= today:
                        aired_by_season[ep.season] = aired_by_season.get(ep.season, 0) + 1

                for s in sorted(by_season):
                    missing_eps = by_season[s]
                    aired_in_season = aired_by_season.get(s, 0)

                    if s not in local_seasons_with_eps:
                        # Zero local episodes for this season → entire season missing
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
                lines.append(f"    {_yellow('?')} Extra (not in TVmaze): {_yellow(ep_list)}")

        lines.append("")

    return "\n".join(lines)


def format_json(reports: list[ShowReport]) -> str:
    def rep_to_dict(r: ShowReport) -> dict:
        return {
            "show_name": r.show_name,
            "matched_title": r.matched_title,
            "tvmaze_id": r.tvmaze_id,
            "status": r.status,
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


def format_csv(reports: list[ShowReport]) -> str:
    import io
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "show_name", "matched_title", "tvmaze_id", "status",
        "local_episodes", "remote_episodes", "missing_count", "ok", "error",
    ])
    for r in sorted(reports, key=lambda x: x.show_name.lower()):
        writer.writerow([
            r.show_name, r.matched_title or "", r.tvmaze_id or "",
            r.status or "", len(r.local_episodes), len(r.remote_episodes),
            len(r.missing), r.ok, r.lookup_error or "",
        ])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global USE_COLOR

    parser = argparse.ArgumentParser(
        description="Compare a local TV library against TVmaze episode data."
    )
    parser.add_argument("root", help="Root directory of the TV library")
    parser.add_argument(
        "--output", choices=["text", "json", "csv"], default="text",
        help="Output format (default: text)"
    )
    parser.add_argument(
        "--missing-only", action="store_true",
        help="Only show shows with missing episodes or errors"
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="Parallel TVmaze API workers (default: 4)"
    )
    parser.add_argument(
        "--outfile", default=None,
        help="Write output to this file instead of stdout"
    )
    parser.add_argument(
        "--no-color", action="store_true",
        help="Disable color output even if the terminal supports it"
    )
    args = parser.parse_args()

    # Set color flag — only for text output to stdout
    if args.output == "text" and not args.outfile and not args.no_color:
        USE_COLOR = _detect_color_support()

    root = Path(args.root).expanduser().resolve()
    print(f"Scanning library: {root}", file=sys.stderr)

    library = scan_local_library(root)
    print(f"Found {len(library)} show directories", file=sys.stderr)

    reports: list[ShowReport] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_show, name, eps): name
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
            print(
                f"  [{done}/{len(library)}] {status_icon} {show_name}"
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
    else:
        output = format_csv(reports)

    if args.outfile:
        Path(args.outfile).write_text(output, encoding="utf-8")
        print(f"Report written to {args.outfile}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
