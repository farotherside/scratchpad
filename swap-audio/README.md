## swap-audio.sh

Walks a directory tree and remuxes video files so English is the first
(default) audio track and Italian is the second. Uses `-c copy`, so nothing is
re-encoded: it runs at disk speed and there is no quality loss.

Other audio tracks keep their original relative order after the first two.
Subtitles and attachments are carried over when present.

### Requirements

`ffmpeg` and `ffprobe` on PATH. Bash 4 or newer (uses `${var,,}` and
associative array indexing).

### Usage

```bash
chmod +x swap-audio.sh

DRY_RUN=1 ./swap-audio.sh /media/films   # report only, change nothing
./swap-audio.sh /media/films             # write to ./swapped/, originals kept
IN_PLACE=1 ./swap-audio.sh /media/films  # overwrite originals
```

### Environment overrides

| Variable    | Default         | Effect                                          |
|-------------|-----------------|-------------------------------------------------|
| `DRY_RUN`   | `0`             | Print the planned mapping, touch nothing         |
| `IN_PLACE`  | `0`             | Overwrite originals instead of mirroring         |
| `OUTDIR`    | `./swapped`     | Destination root when not in place               |
| `EXTS`      | `mkv mp4 m4v`   | Extensions to process, space separated           |
| `PRIMARY`   | `eng`           | Language that ends up first                      |
| `SECONDARY` | `ita`           | Language that ends up second                     |

`PRIMARY` and `SECONDARY` accept the common variants, so `en`, `eng` and
`english` all resolve to the same thing.

### How it picks tracks

Track selection is driven by the `language` metadata tag, read with `ffprobe`,
not by track position. The first stream matching each wanted language wins.
Files are skipped with a note, rather than processed badly, when:

* fewer than two audio tracks are present
* either language is missing from the tags
* the primary language is already the first audio track

Untagged files report as `und`. If a whole library comes back that way, the
tags are missing and selection has to be done by position instead.

### Safety

Default mode mirrors the tree into `OUTDIR` and leaves originals alone. In
place mode writes to a temp file in the same directory, checks the result is
not obviously truncated, then moves it over the original. Run a dry pass
first, spot check a couple of results, then commit to the rest.

### Known limits

* Cover art and attached pictures ride along under `-map 0:v`.
* MP4 has no attachment streams, so `-map 0:t?` is a no-op there.
* Exit status is non-zero if any file failed.
