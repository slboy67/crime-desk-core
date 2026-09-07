#!/usr/bin/env python3
"""colors.py — terminal visual helpers for the HUMAN render path only.

Native capabilities emit JSON (the machine path) and never touch this. The human
render of a capability (e.g. `triage.py` with no `--json`) uses it for ANSI color
+ unicode bars/arrows/dots. ANSI is gated (off when piped / NO_COLOR / --no-color);
unicode markers render everywhere. Ported from the parts-bin shared util.
"""
import os
import sys

ENABLED = None  # None = auto-detect; set_enabled() overrides


def set_enabled(val):
    global ENABLED
    ENABLED = val


def want_color(stream=None):
    if ENABLED is not None:
        return ENABLED
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR_FORCE"):
        return True
    s = stream or sys.stdout
    return bool(getattr(s, "isatty", lambda: False)())


_CODES = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "magenta": "\033[95m", "cyan": "\033[96m",
    "white": "\033[97m", "grey": "\033[90m",
}


def c(text, *styles):
    if not want_color():
        return str(text)
    pre = "".join(_CODES.get(s, "") for s in styles)
    return f"{pre}{text}{_CODES['reset']}" if pre else str(text)


def dot(level):
    """level: 'live' | 'watch' | 'dust' | 'long' | 'short'."""
    return {"live": "🔴", "watch": "🟠", "dust": "⚪", "long": "🟢", "short": "🔴"}.get(level, "⚪")


def bar(pct, width=10):
    if pct is None:
        return " " * (width + 2)
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "▕" + "█" * filled + "░" * (width - filled) + "▏"


def arrow(v, eps=0.0):
    if v is None:
        return "·"
    if v > eps:
        return "▲"
    if v < -eps:
        return "▼"
    return "▏"


def sign_color(text, v, flip=False):
    if v is None:
        return c(text, "grey")
    pos, neg = ("red", "green") if flip else ("green", "red")
    return c(text, pos if v > 0 else neg if v < 0 else "grey")
