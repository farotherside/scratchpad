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
def search_show(show_name: str) -> Optional[dict]:
    """Return best TVmaze show match or None."""
    results = _request(f"{TVMAZE_BASE}/search/shows", params={"q": show_name})
    if not results:
        return None
    # TVmaze returns results sorted by relevance score
    return results[0]["show"]


def fetch_episodes(tvmaze_id: int) -> list[RemoteEpisode]:
    """Fetch full episode list for a show."""
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
    return episodes


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
    try:
        show = search_show(show_name)
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
    lines.append(f"TV Library Report — {len(reports)} shows scanned")
    lines.append(f"{'─' * 60}")
    lines.append(f"  ✓ Complete:  {ok_count}")
    lines.append(f"  ✗ Issues:    {len(reports) - ok_count}")
    lines.append("")

    for report in sorted(reports, key=lambda r: r.show_name.lower()):
        if missing_only and report.ok:
            continue

        header = f"► {report.show_name}"
        if report.matched_title and report.matched_title != report.show_name:
            header += f"  [TVmaze: {report.matched_title}]"
        if report.status:
            header += f"  ({report.status})"
        lines.append(header)

        if report.lookup_error:
            lines.append(f"    ⚠ {report.lookup_error}")
            lines.append("")
            continue

        local_count = len(report.local_episodes)
        remote_count = len(report.remote_episodes)
        lines.append(
            f"    Local: {local_count} episodes  |  "
            f"TVmaze (aired): {remote_count} episodes"
        )

        if not report.missing and not report.extra:
            lines.append("    ✓ All aired episodes present")
        else:
            if report.missing:
                by_season: dict[int, list[RemoteEpisode]] = {}
                for ep in report.missing:
                    by_season.setdefault(ep.season, []).append(ep)
                for s in sorted(by_season):
                    eps = by_season[s]
                    ep_nums = ", ".join(_season_ep(e) for e in sorted(eps, key=lambda x: x.episode))
                    lines.append(f"    ✗ Missing S{s:02d}: {ep_nums}")
            if report.extra:
                ep_list = ", ".join(_season_ep(e) for e in sorted(report.extra, key=lambda x: (x.season, x.episode)))
                lines.append(f"    ? Extra (not in TVmaze): {ep_list}")

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
    args = parser.parse_args()

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
