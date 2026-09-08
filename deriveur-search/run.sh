#!/bin/sh
# YachtScout cron wrapper.
#
#   ./run.sh                 run and publish
#   PUBLISH_DIR=~/public_html/boats ./run.sh
#
# Safe to run from cron on a shared host: single-instance lock, no root, and
# it never leaves a half-written file in the web directory.

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PUBLISH_DIR="${PUBLISH_DIR:-$HOME/public_html/boats}"
PYTHON="${PYTHON:-python3}"
LOCK="$HERE/.run.lock"
LOG="$HERE/state/run.log"

mkdir -p "$HERE/state"

# mkdir is atomic everywhere; flock is not always available on shared hosts.
if ! mkdir "$LOCK" 2>/dev/null; then
    if [ -f "$LOCK/pid" ] && kill -0 "$(cat "$LOCK/pid")" 2>/dev/null; then
        echo "$(date -u +%FT%TZ) already running (pid $(cat "$LOCK/pid")), exiting" >> "$LOG"
        exit 0
    fi
    echo "$(date -u +%FT%TZ) clearing stale lock" >> "$LOG"
    rm -rf "$LOCK"; mkdir "$LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT INT TERM

{
    echo "=== $(date -u +%FT%TZ) start"
    "$PYTHON" "$HERE/yachtscout.py" "$@"
    echo "=== $(date -u +%FT%TZ) done"
} >> "$LOG" 2>&1

# Publish atomically: write to temp names in the target dir, then rename.
mkdir -p "$PUBLISH_DIR"
for f in brief.md listings.json shortlist.json changes.json run_report.json index.html; do
    if [ -f "$HERE/out/$f" ]; then
        cp "$HERE/out/$f" "$PUBLISH_DIR/.$f.tmp"
        mv "$PUBLISH_DIR/.$f.tmp" "$PUBLISH_DIR/$f"
    fi
done

# Keep the log from growing without bound on a shared account.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 1000000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

echo "published to $PUBLISH_DIR"
