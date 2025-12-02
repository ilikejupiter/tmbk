# -*- coding: utf-8 -*-
"""
General utilities (terminal, formatting, config).
Compatible with Python 3.9.18 (stdlib only; python-dotenv optional).
"""

from __future__ import annotations

import contextlib
import html
import logging
import os
import platform
import re
import sys
from typing import Any, Mapping, Optional, Union

logger = logging.getLogger(__name__)

__all__ = [
    "clear_screen",
    "pause",
    "format_quota_byte",
    "display_html",
    "ensure_api_key",
    "verify_api_key",
    "get_api_key",
]

# =============================================================================
# OPTIONAL .env LOADING (NO HARD DEPENDENCY)
# =============================================================================

def _try_load_dotenv() -> None:
    """
    Load environment variables from .env if python-dotenv is installed.
    This keeps the module stable in environments without dotenv.
    """
    with contextlib.suppress(Exception):
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()

_try_load_dotenv()

# =============================================================================
# TERMINAL UTILITIES
# =============================================================================

def _isatty() -> bool:
    """
    True if both stdin and stdout look like an interactive terminal.
    Safer than only checking stdin.
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty() and sys.stdout and sys.stdout.isatty())
    except Exception:
        return False


def clear_screen(*, force: bool = False) -> None:
    """
    Clear terminal screen (cross-platform).
    - In non-interactive environments (CI/daemon), no-op unless force=True.
    - Uses ANSI escape when possible, falls back to system command.
    """
    if not force and not _isatty():
        return

    try:
        # Prefer ANSI for speed and to avoid shelling out.
        # Works in most modern terminals (including Windows Terminal).
        sys.stdout.write("\033[2J\033[H")  # clear + cursor home
        sys.stdout.flush()
        return
    except Exception:
        pass

    try:
        cmd = "cls" if platform.system().lower().startswith("win") else "clear"
        os.system(cmd)
    except Exception:
        # Last-resort fallback
        print("\n" * 50)


def pause(message: str = "Tekan [Enter] untuk melanjutkan...") -> None:
    """
    Pause program execution.
    - In non-TTY environments, no-op to avoid hanging pipelines.
    """
    if not _isatty():
        return
    try:
        print("")
        input(message)
    except (KeyboardInterrupt, EOFError):
        pass

# =============================================================================
# FORMATTING UTILITIES
# =============================================================================

_Number = Union[int, float, str]

def _to_float(value: Any, default: float = 0.0) -> float:
    """
    Robust-ish float coercion.
    Accepts:
      - int/float/bool
      - str numeric (supports "1,234.56" and "1.234,56" heuristics)
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return float(int(value))
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip().replace(" ", "").replace("_", "")
        if not s:
            return default

        sign = 1.0
        if s[0] in "+-":
            if s[0] == "-":
                sign = -1.0
            s = s[1:]

        # Both separators: decide decimal separator by last occurrence.
        if "." in s and "," in s:
            last_dot = s.rfind(".")
            last_comma = s.rfind(",")
            if last_comma > last_dot:
                # "1.234,56" -> thousands "."; decimal ","
                s = s.replace(".", "").replace(",", ".")
            else:
                # "1,234.56" -> thousands ","; decimal "."
                s = s.replace(",", "")

        # Single comma: could be decimal or thousand
        elif "," in s and "." not in s:
            # "1234,56" -> decimal comma
            if s.count(",") == 1 and len(s.split(",", 1)[1]) in (1, 2):
                s = s.replace(",", ".")
            else:
                s = s.replace(",", "")

        # else: normal "1234.56" / "1234"
        try:
            return sign * float(s)
        except ValueError:
            return default

    return default


def format_quota_byte(size: _Number) -> str:
    """
    Convert bytes to human-readable format (B, KB, MB, GB, TB, PB).
    Example: 1048576 -> '1.00 MB'
    """
    size_f = _to_float(size, default=0.0)
    if size_f < 0:
        size_f = 0.0

    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    power = 1024.0
    idx = 0

    while size_f >= power and idx < (len(units) - 1):
        size_f /= power
        idx += 1

    # For bytes, decimals look weird: "12.00 B" -> "12 B"
    if units[idx] == "B":
        return f"{int(size_f)} B"
    return f"{size_f:.2f} {units[idx]}"

# =============================================================================
# HTML DISPLAY (SANITIZE TO TERMINAL TEXT)
# =============================================================================

# Precompiled regexes for speed & stability
_RE_SCRIPT_STYLE = re.compile(r"<(script|style)\b.*?>.*?</\1>", re.IGNORECASE | re.DOTALL)
_RE_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_BLOCK_CLOSE = re.compile(
    r"</(p|div|section|article|header|footer|main|aside|nav|tr|table|ul|ol|h[1-6])\s*>",
    re.IGNORECASE,
)
_RE_LI_OPEN = re.compile(r"<li(\s[^>]*)?>", re.IGNORECASE)
_RE_LI_CLOSE = re.compile(r"</li\s*>", re.IGNORECASE)
_RE_A = re.compile(r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_RE_TAGS = re.compile(r"<[^>]+>")
_RE_WS_LINES = re.compile(r"[ \t]+\n")
_RE_MULTI_NEWLINES = re.compile(r"\n{3,}")
_RE_MULTI_SPACES = re.compile(r"[ \t]{2,}")


def display_html(raw_html: str) -> str:
    """
    Clean HTML to readable terminal text.

    Steps:
    - Unescape HTML entities
    - Remove <script>/<style> content
    - Convert links: <a href="url">text</a> -> "text (url)"
    - Convert <br> -> newline
    - Convert closing block tags -> blank line
    - Convert <li> -> bullet
    - Strip remaining tags + tidy whitespace/newlines
    """
    if not raw_html:
        return "Tidak ada deskripsi."

    try:
        text = html.unescape(raw_html)
        # Remove script/style blocks
        text = _RE_SCRIPT_STYLE.sub("", text)

        # Link transform
        def _a_sub(m: re.Match) -> str:
            href = (m.group(1) or "").strip()
            inner = (m.group(2) or "")
            inner = _RE_TAGS.sub("", inner)
            inner = html.unescape(inner).strip()
            if not inner:
                inner = href or "link"
            if href:
                return f"{inner} ({href})"
            return inner

        text = _RE_A.sub(_a_sub, text)

        # Line breaks and block closings
        text = _RE_BR.sub("\n", text)
        text = _RE_BLOCK_CLOSE.sub("\n\n", text)

        # Bullet lists
        text = _RE_LI_OPEN.sub("\n • ", text)
        text = _RE_LI_CLOSE.sub("", text)

        # Strip remaining tags
        text = _RE_TAGS.sub("", text)

        # Normalize whitespace
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        text = text.replace("\u00a0", " ")  # nbsp -> space
        text = _RE_WS_LINES.sub("\n", text)
        text = _RE_MULTI_SPACES.sub(" ", text)
        text = _RE_MULTI_NEWLINES.sub("\n\n", text)

        text = text.strip()
        return text if text else "Tidak ada deskripsi."
    except Exception as e:
        logger.exception("Error parsing HTML: %s", e)
        # fallback: return raw to avoid breaking flow
        return raw_html

# =============================================================================
# CONFIGURATION UTILITIES
# =============================================================================

_ENV_KEYS = ("API_KEY", "XL_API_KEY", "MYXL_API_KEY")

def get_api_key(*, strict: bool = False) -> str:
    """
    Get API key from environment.
    - strict=False: return "" if not found (backwards compatible)
    - strict=True : raise PayloadValidationError-like ValueError with clear message
    """
    for key_name in _ENV_KEYS:
        val = os.getenv(key_name)
        if val and val.strip():
            return val.strip()

    if strict:
        raise ValueError(f"API key tidak ditemukan. Set salah satu env var: {', '.join(_ENV_KEYS)}")
    logger.warning("API key tidak ditemukan di environment (.env atau sistem).")
    return ""


def ensure_api_key() -> str:
    """
    Backwards compatible wrapper.
    Return api key if exists; else return "" (does not raise).
    """
    return get_api_key(strict=False)


def verify_api_key(api_key: str) -> bool:
    """
    Basic API key validation:
    - minimal length
    - no whitespace/control characters
    - not overly restrictive (compatible with many backends)
    """
    if not api_key:
        return False
    s = api_key.strip()
    if len(s) < 8:
        return False
    # Disallow whitespace/control chars inside the key
    for ch in s:
        if ch.isspace() or ord(ch) < 32:
            return False
    return True


if __name__ == "__main__":
    # Small self-checks
    print(format_quota_byte(0))
    print(format_quota_byte(1024))
    print(format_quota_byte("1.234,56"))
    print(display_html("<p>Hello<br>World</p><ul><li>A</li><li><a href='https://x.com'>X</a></li></ul>"))
    k = ensure_api_key()
    print("API key exists?", bool(k), "valid?", verify_api_key(k) if k else False)