"""
terminal.py — Renders a float32 luminance framebuffer to the terminal.

Two backends, tried in order:
  1. aalib  — proper ASCII art library with dithering/contrast control
  2. curses — built-in; uses a dense ASCII brightness ramp

Handles SIGWINCH for live terminal resize.
"""

import curses
import os
import signal
import sys
import time
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

    def render(self, framebuf: np.ndarray, cols: int, rows: int) -> str:
        """Downscale framebuf to (rows, cols) and map luminance → chars."""
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

    def __init__(self, use_aalib: bool = True, ramp: str = _RAMP_70):
        self._use_aalib = use_aalib
        self._ramp = ramp
        self._stdscr = None
        self._renderer = None

    def __enter__(self):
        # Attempt aalib first — validate it actually works with a small test render
        if self._use_aalib:
            try:
                candidate = AalibRenderer()
                # Smoke-test: render a tiny 4x2 buffer to catch runtime failures
                test_buf = np.zeros((2, 4), dtype=np.float32)
                candidate.render(test_buf, 4, 2)
                self._renderer = candidate
            except Exception:
                self._use_aalib = False

        if not self._use_aalib:
            self._renderer = FallbackRenderer(self._ramp)

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

    def show(self, framebuf: np.ndarray, status_line: str = ""):
        """Render framebuf to the terminal window."""
        self.check_resize()
        rows = self._rows
        cols = self._cols

        # Leave one row for status line if provided
        render_rows = rows - 1 if status_line else rows

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
