"""
terminal.py — Renders a float32 luminance framebuffer to the terminal.

Two backends, tried in order:
  1. aalib  — proper ASCII art library with dithering/contrast control
  2. curses — built-in; uses a dense ASCII brightness ramp

Handles SIGWINCH for live terminal resize.
"""

import curses
import os
import random
import signal
import sys
import time
from collections import deque
from typing import Optional
import numpy as np


# ---------------------------------------------------------------------------
# ASCII brightness ramps
# ---------------------------------------------------------------------------
# Dense 70-char ramp (darkest → brightest)
_RAMP_70 = (
    " `.-':_,^=;><+!rc*/z?sLTv)J7(|Fi{C}fI31tlu[neoZ5Yxjya]2ESwqkP6h9d4VpOGbUAKXHm8RD#$Bg0MNWQ%&@"
)
# Compact 10-char ramp (easier on terminals that struggle with dense art)
_RAMP_10 = " .:-=+*#%@"


def _lum_to_char(lum: float, ramp: str) -> str:
    idx = int(lum * (len(ramp) - 1))
    return ramp[max(0, min(len(ramp) - 1, idx))]


# ---------------------------------------------------------------------------
# aalib backend
# ---------------------------------------------------------------------------
def _try_import_aalib():
    try:
        import aalib
        return aalib
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Terminal size
# ---------------------------------------------------------------------------
def get_terminal_size() -> tuple[int, int]:
    """Returns (cols, rows) — character dimensions of the terminal."""
    try:
        sz = os.get_terminal_size()
        return sz.columns, sz.lines
    except OSError:
        return 80, 24


# ---------------------------------------------------------------------------
# Resize flag — set by SIGWINCH handler
# ---------------------------------------------------------------------------
_resize_pending = False

def _on_sigwinch(sig, frame):
    global _resize_pending
    _resize_pending = True


# ---------------------------------------------------------------------------
# Debug info helpers
# ---------------------------------------------------------------------------

def _detect_terminal() -> str:
    """Best-effort terminal emulator identification."""
    for var in ("TERM_PROGRAM", "TERMINAL_EMULATOR", "TERM"):
        val = os.environ.get(var, "")
        if val:
            return val
    return "unknown"


def _detect_colors() -> str:
    """Detect color capability from environment."""
    colorterm = os.environ.get("COLORTERM", "").lower()
    if colorterm in ("truecolor", "24bit"):
        return "24-bit (truecolor)"
    if colorterm == "256color":
        return "256-color"
    term = os.environ.get("TERM", "")
    if "256color" in term:
        return "256-color"
    if "color" in term:
        return "8-color"
    # Ask curses at runtime if available
    try:
        n = curses.tigetnum("colors")
        if n >= 16777216:
            return "24-bit (truecolor)"
        if n >= 256:
            return "256-color"
        if n >= 8:
            return f"{n}-color"
    except Exception:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Static / noise effect  (inspired by BB demo's scene1.c)
#
# BB writes directly into aalib's textbuffer/attrbuffer using HEXA chars
# (random hex digits or spaces in a 3-wide pattern) with per-row random
# horizontal shifts and a randomval parameter on the render pipeline.
#
# We replicate the same idea at the character level: after downscaling the
# framebuffer to (rows, cols), we identify background pixels (luminance below
# a threshold) and overwrite them with noise characters using the same
# 3-wide hex pattern + random per-row shifts.
# ---------------------------------------------------------------------------

# Characters used for noise — hex-like, visually dense
_HEXA = "0123456789ABCDEF"
_STATIC_BG_CHARS = " " * 6 + "." * 3 + "`" * 2 + _HEXA   # weighted toward sparse


def _make_static_row(cols: int, shift: int, intensity: float,
                     rng: np.random.Generator) -> list[str]:
    """Generate one row of BB-style hex static.

    intensity: 0.0 = silent, 1.0 = full noise
    shift:     horizontal offset for the 3-wide pattern (0-2)
    """
    chars = []
    for x in range(cols):
        if rng.random() > intensity:
            chars.append(" ")   # sparse — leave space
            continue
        # BB pattern: every 3rd column (offset by shift) gets a hex char
        if (x - shift) % 3 == 0:
            chars.append(_HEXA[rng.integers(0, len(_HEXA))])
        else:
            chars.append(" ")
    return chars


def _apply_static(lines: list[str], small: np.ndarray,
                  intensity: float, rng: np.random.Generator,
                  bg_threshold: float = 0.08) -> list[str]:
    """Overwrite background positions in rendered ASCII lines with noise.

    lines      : list of rendered ASCII strings, one per row
    small      : float32 (rows, cols) luminance buffer already downscaled
    intensity  : 0..1 static intensity
    bg_threshold: pixels below this luminance are considered background
    """
    if intensity <= 0.0:
        return lines

    rows, cols = small.shape
    out = []
    for r, line in enumerate(lines[:rows]):
        row_lum = small[r]                          # (cols,) float32
        bg_mask = row_lum < bg_threshold            # True where background
        if not bg_mask.any():
            out.append(line)
            continue

        # Random per-row horizontal shift (BB's randshift)
        shift = rng.integers(0, 3) if rng.random() < intensity else 0
        noise = _make_static_row(cols, shift, intensity, rng)

        chars = list(line.ljust(cols)[:cols])
        for x in range(min(cols, len(bg_mask))):
            if bg_mask[x]:
                chars[x] = noise[x]
        out.append("".join(chars))
    return out


# ---------------------------------------------------------------------------
# Renderer classes
# ---------------------------------------------------------------------------

class AalibRenderer:
    """Renders using the aalib library (requires both aalib and Pillow)."""

    def __init__(self):
        self._aa = _try_import_aalib()
        if self._aa is None:
            raise ImportError("aalib not available")
        try:
            from PIL import Image  # noqa: F401 — validate at init time
            self._Image = Image
        except ImportError:
            raise ImportError("Pillow (PIL) not available — needed by aalib backend")

    def render(self, framebuf: np.ndarray, cols: int, rows: int) -> str:
        """Convert float32 (H, W) framebuf → ASCII string via aalib."""
        aa = self._aa
        Image = self._Image
        u8 = (np.clip(framebuf, 0, 1) * 255).astype(np.uint8)
        screen = aa.AsciiScreen(width=cols, height=rows)
        vw, vh = screen.virtual_size
        img = Image.fromarray(u8, mode="L").resize((vw, vh), Image.LANCZOS)
        screen.put_image((0, 0), img)
        result = screen.render()
        # render() returns bytes on some aalib versions
        if isinstance(result, bytes):
            result = result.decode("ascii", errors="replace")
        return result


class FallbackRenderer:
    """Renders using a pure-Python ASCII ramp — no dependencies."""

    def __init__(self, ramp: str = _RAMP_70):
        self._ramp = ramp
        self._rng = np.random.default_rng()

    def render(self, framebuf: np.ndarray, cols: int, rows: int,
               static: float = 0.0) -> str:
        """Downscale framebuf to (rows, cols) and map luminance → chars.

        static: 0.0 = no noise, 1.0 = full BB-style hex static in background
        """
        h, w = framebuf.shape

        # Bilinear downscale via numpy slicing (fast approximation)
        row_idx = np.linspace(0, h - 1, rows).astype(int)
        col_idx = np.linspace(0, w - 1, cols).astype(int)
        small = framebuf[np.ix_(row_idx, col_idx)]  # (rows, cols)

        # Terminal characters are taller than wide; compensate by sampling
        # every other row (aspect correction factor ~0.5)
        lines = []
        ramp = self._ramp
        ramp_len = len(ramp) - 1
        for row in small:
            chars = "".join(ramp[int(v * ramp_len)] for v in row)
            lines.append(chars)

        # Apply BB-style background static if requested
        if static > 0.0:
            lines = _apply_static(lines, small, static, self._rng)

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal display loop
# ---------------------------------------------------------------------------

class TerminalDisplay:
    """
    Manages the curses window and dispatches rendered frames.

    Usage:
        with TerminalDisplay() as td:
            while running:
                buf = render(...)
                td.show(buf)
                time.sleep(frame_delay)
    """

    def __init__(self, use_aalib: bool = True, ramp: str = _RAMP_70,
                 static: float = 0.0):
        self._use_aalib = use_aalib
        self._ramp = ramp
        self._stdscr = None
        self._renderer = None
        self.static = static          # 0.0 = off, 1.0 = full BB static
        # FPS tracking
        self._frame_times: deque = deque(maxlen=30)
        # Debug info (populated in __enter__)
        self.debug_backend: str = "unknown"
        self.debug_terminal: str = _detect_terminal()
        self.debug_colors: str = "unknown"  # populated after curses init

    def __enter__(self):
        # Attempt aalib first — validate it actually works with a small test render
        self._aalib_error: str = ""
        if self._use_aalib:
            try:
                candidate = AalibRenderer()
                # Smoke-test: render a tiny 4x2 buffer to catch runtime failures
                test_buf = np.zeros((2, 4), dtype=np.float32)
                candidate.render(test_buf, 4, 2)
                self._renderer = candidate
                self.debug_backend = "aalib"
            except Exception as e:
                self._aalib_error = f"{type(e).__name__}: {e}"
                self._use_aalib = False

        if not self._use_aalib:
            self._renderer = FallbackRenderer(self._ramp)
            err = f" [{self._aalib_error}]" if self._aalib_error else ""
            self.debug_backend = f"fallback (ramp/{len(self._ramp)}ch){err}"

        # Set up curses
        self._stdscr = curses.initscr()
        curses.noecho()
        curses.cbreak()
        try:
            curses.curs_set(0)   # hide cursor (not supported on all terminals)
        except curses.error:
            pass
        self._stdscr.nodelay(True)   # non-blocking getch
        self._stdscr.keypad(True)

        # SIGWINCH for resize
        signal.signal(signal.SIGWINCH, _on_sigwinch)

        self._cols, self._rows = get_terminal_size()

        # Color detection — best done after curses init
        self.debug_colors = _detect_colors()

        return self

    def __exit__(self, *_):
        if self._stdscr:
            curses.nocbreak()
            self._stdscr.keypad(False)
            curses.echo()
            curses.endwin()

    def poll_input(self) -> Optional[int]:
        """Return a keycode if pressed, else None."""
        if self._stdscr is None:
            return None
        try:
            return self._stdscr.getch()
        except Exception:
            return None

    def check_resize(self) -> bool:
        """Return True if terminal was resized (and update internal dims)."""
        global _resize_pending
        if _resize_pending:
            _resize_pending = False
            self._cols, self._rows = get_terminal_size()
            if self._stdscr:
                self._stdscr.clear()
                curses.resizeterm(self._rows, self._cols)
            return True
        return False

    @property
    def cols(self) -> int:
        return self._cols

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def fps(self) -> float:
        """Current smoothed FPS over the last 30 frames."""
        ft = self._frame_times
        if len(ft) < 2:
            return 0.0
        span = ft[-1] - ft[0]
        return (len(ft) - 1) / span if span > 0 else 0.0

    @property
    def debug_line(self) -> str:
        """One-line debug summary: backend | terminal | colors | fps."""
        return (
            f"renderer={self.debug_backend}  "
            f"term={self.debug_terminal}  "
            f"colors={self.debug_colors}  "
            f"fps={self.fps:.1f}"
        )

    def show(self, framebuf: np.ndarray, status_line: str = "",
             debug_line: str = ""):
        """Render framebuf to the terminal window.

        status_line  — displayed on the last row
        debug_line   — displayed on the second-to-last row (renderer/fps info)
        """
        self._frame_times.append(time.monotonic())
        self.check_resize()
        rows = self._rows
        cols = self._cols

        # Reserve rows for overlay lines
        overlay_rows = (1 if status_line else 0) + (1 if debug_line else 0)
        render_rows = max(1, rows - overlay_rows)

        # Pass static intensity to FallbackRenderer; aalib renderer ignores it
        if hasattr(self._renderer, 'render') and isinstance(self._renderer, FallbackRenderer):
            art = self._renderer.render(framebuf, cols, render_rows, static=self.static)
        else:
            art = self._renderer.render(framebuf, cols, render_rows)
        lines = art.split("\n")

        if self._stdscr:
            scr = self._stdscr
            scr.erase()
            for i, line in enumerate(lines[:render_rows]):
                try:
                    scr.addstr(i, 0, line[:cols])
                except curses.error:
                    pass
            # Debug line sits above status line
            if debug_line and status_line:
                try:
                    scr.addstr(rows - 2, 0, debug_line[:cols])
                except curses.error:
                    pass
            elif debug_line:
                try:
                    scr.addstr(rows - 1, 0, debug_line[:cols])
                except curses.error:
                    pass
            if status_line:
                try:
                    scr.addstr(rows - 1, 0, status_line[:cols])
                except curses.error:
                    pass
            scr.refresh()
        else:
            # Fallback: just print (no curses)
            sys.stdout.write("\033[H")  # move to top-left
            sys.stdout.write(art)
            if debug_line:
                sys.stdout.write(f"\n{debug_line}")
            if status_line:
                sys.stdout.write(f"\n{status_line}")
            sys.stdout.flush()


# ---------------------------------------------------------------------------
# Standalone render-to-string (for non-interactive use / testing)
# ---------------------------------------------------------------------------

def framebuf_to_str(framebuf: np.ndarray, cols: int, rows: int,
                    ramp: str = _RAMP_70) -> str:
    r = FallbackRenderer(ramp)
    return r.render(framebuf, cols, rows)
