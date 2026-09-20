#!/usr/bin/env bash
#
# swap-audio.sh
#
# Walks a directory tree and remuxes video files so that English is the first
# (default) audio track and Italian is the second. All other streams are kept
# in their original order. Nothing is re-encoded, so this is fast and lossless.
#
# Usage:
#   ./swap-audio.sh [ROOT_DIR]
#
# Environment overrides:
#   DRY_RUN=1     show what would happen, change nothing        (default 0)
#   IN_PLACE=1    overwrite the originals instead of mirroring   (default 0)
#   OUTDIR=path   where the mirrored tree is written        (default ./swapped)
#   EXTS="mkv mp4 m4v"   file extensions to process
#   PRIMARY=eng   language that should end up first
#   SECONDARY=ita language that should end up second
#
# Examples:
#   DRY_RUN=1 ./swap-audio.sh /media/films
#   ./swap-audio.sh /media/films
#   IN_PLACE=1 ./swap-audio.sh /media/films
#

set -uo pipefail

ROOT="${1:-.}"
ROOT="${ROOT%/}"
DRY_RUN="${DRY_RUN:-0}"
IN_PLACE="${IN_PLACE:-0}"
OUTDIR="${OUTDIR:-./swapped}"
EXTS="${EXTS:-mkv mp4 m4v}"
PRIMARY="${PRIMARY:-eng}"
SECONDARY="${SECONDARY:-ita}"

command -v ffmpeg  >/dev/null || { echo "ffmpeg not found in PATH"  >&2; exit 1; }
command -v ffprobe >/dev/null || { echo "ffprobe not found in PATH" >&2; exit 1; }
[[ -d "$ROOT" ]] || { echo "not a directory: $ROOT" >&2; exit 1; }

swapped=0; skipped=0; failed=0

# Fold the common variants of a language tag down to one ISO 639-2 code.
norm_lang() {
  case "${1,,}" in
    en|eng|en-us|en-gb|english)  echo eng ;;
    it|ita|it-it|italian)        echo ita ;;
    "")                          echo und ;;
    *)                           echo "${1,,}" ;;
  esac
}

process() {
  local f="$1"
  local ext="${f##*.}"

  # Collect the audio streams: absolute index plus language tag.
  local -a idx=() lang=()
  local line i l
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    i="${line%%,*}"
    l="${line#*,}"
    [[ "$l" == "$line" ]] && l=""     # no comma means the tag was missing
    idx+=("$i")
    lang+=("$(norm_lang "$l")")
  done < <(ffprobe -v error -select_streams a \
             -show_entries stream=index:stream_tags=language \
             -of csv=p=0 "$f" 2>/dev/null)

  if (( ${#idx[@]} < 2 )); then
    printf 'skip  %s  (only %d audio track(s))\n' "$f" "${#idx[@]}"
    (( skipped++ )); return
  fi

  # Locate the first stream matching each wanted language.
  local p=-1 s=-1 n
  for n in "${!idx[@]}"; do
    [[ $p -lt 0 && "${lang[$n]}" == "$(norm_lang "$PRIMARY")"   ]] && p=$n
    [[ $s -lt 0 && "${lang[$n]}" == "$(norm_lang "$SECONDARY")" ]] && s=$n
  done

  if (( p < 0 || s < 0 )); then
    printf 'skip  %s  (tags found: %s)\n' "$f" "${lang[*]}"
    (( skipped++ )); return
  fi

  if (( p == 0 )); then
    printf 'skip  %s  (%s is already first)\n' "$f" "$PRIMARY"
    (( skipped++ )); return
  fi

  # Build the stream map: video, primary audio, secondary audio, any other
  # audio in its original order, then subtitles and attachments if present.
  local -a maps=(-map 0:v)
  maps+=(-map "0:${idx[$p]}" -map "0:${idx[$s]}")
  for n in "${!idx[@]}"; do
    (( n == p || n == s )) && continue
    maps+=(-map "0:${idx[$n]}")
  done
  maps+=(-map '0:s?' -map '0:t?')

  # First audio track becomes the default, every other one loses the flag.
  local -a disp=(-disposition:a:0 default)
  for (( n = 1; n < ${#idx[@]}; n++ )); do
    disp+=(-disposition:a:$n 0)
  done

  # Decide where the result goes.
  local out tmp rel
  if (( IN_PLACE )); then
    tmp="${f%.*}.swapaudio.tmp.$ext"
    out="$tmp"
  else
    rel="${f#"$ROOT"/}"
    out="$OUTDIR/$rel"
  fi

  if (( DRY_RUN )); then
    printf 'would  %s  ->  a:0=%s(stream %s)  a:1=%s(stream %s)  ->  %s\n' \
      "$f" "$PRIMARY" "${idx[$p]}" "$SECONDARY" "${idx[$s]}" \
      "$( (( IN_PLACE )) && echo "(in place)" || echo "$out" )"
    (( skipped++ )); return
  fi

  mkdir -p "$(dirname "$out")" || { (( failed++ )); return; }

  if ! ffmpeg -nostdin -v error -i "$f" \
        "${maps[@]}" -c copy "${disp[@]}" -y "$out"; then
    printf 'FAIL  %s\n' "$f" >&2
    [[ -f "$out" ]] && rm -f "$out"
    (( failed++ )); return
  fi

  # Sanity check before touching the original: output should be a similar size.
  local sz_in sz_out
  sz_in=$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f")
  sz_out=$(stat -c %s "$out" 2>/dev/null || stat -f %z "$out")
  if (( sz_out < sz_in / 2 )); then
    printf 'FAIL  %s  (output looks truncated: %s vs %s bytes)\n' \
      "$f" "$sz_out" "$sz_in" >&2
    rm -f "$out"
    (( failed++ )); return
  fi

  if (( IN_PLACE )); then
    mv -f "$tmp" "$f" || { (( failed++ )); return; }
    printf 'ok    %s\n' "$f"
  else
    printf 'ok    %s  ->  %s\n' "$f" "$out"
  fi
  (( swapped++ ))
}

# Assemble the find expression from the extension list.
find_args=()
first=1
for e in $EXTS; do
  (( first )) && find_args+=( \( -iname "*.$e" ) || find_args+=( -o -iname "*.$e" )
  first=0
done
find_args+=( \) )

while IFS= read -r -d '' file; do
  process "$file"
done < <(find "$ROOT" -type f "${find_args[@]}" -print0 | sort -z)

printf '\ndone: %d swapped, %d skipped, %d failed\n' "$swapped" "$skipped" "$failed"
(( failed == 0 ))
