"""
util.py — terminal + formatting + HTML-to-text helpers (Python 3.9+)

Changes vs original:
- Safe optional dotenv loading (won't crash if python-dotenv isn't installed)
- Logging: library-friendly (NullHandler) + optional default config
- More robust terminal clear + width detection
- More stable bytes formatter (handles strings like "1,024", negatives)
- HTML -> text: better whitespace normalization + bullet wrapping
"""

from __future__ import annotations

import html as _html
import logging
import os
import platform
import re
import shutil
import sys
import textwrap
from html.parser import HTMLParser
from typing import Optional, Union

# =============================================================================
# ENV (.env) LOADING (SAFE / OPTIONAL)
# =============================================================================

def try_load_dotenv(*, override: bool = False) -> bool:
    """
    Try loading environment variables from a .env file.
    Returns True if python-dotenv is available and load executed, else False.

    The loader is intentionally optional to keep this module stable in minimal envs.
    """
    # Allow disabling auto-load by env var.
    # Accept: "0", "false", "no" (case-insensitive)
    auto = os.getenv("MYXL_AUTO_LOAD_DOTENV", "1").strip().lower()
    if auto in {"0", "false", "no"}:
        return False

    try:
        from dotenv import load_dotenv  # type: ignore
    except Exception:
        return False

    try:
        load_dotenv(override=override)
        return True
    except Exception:
        return False


# Auto-load like the original script, but safely.
try_load_dotenv()


# =============================================================================
# LOGGING
# =============================================================================

logger = logging.getLogger("myxl.utils")
# Library-safe default: don't emit "No handler found" warnings.
logger.addHandler(logging.NullHandler())


def configure_default_logging(
    level: int = logging.INFO,
    *,
    force: bool = False,
    name: str = "myxl.utils",
) -> logging.Logger:
    """
    Configure a reasonable default logging format if the application hasn't
    configured logging yet.

    - force=False avoids overriding app logging config.
    - If you want to ensure it always applies, set force=True.
    """
    root = logging.getLogger()
    if force or not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )
    return logging.getLogger(name)


# =============================================================================
# TERMINAL UTILITIES
# =============================================================================

CLI_WIDTH_MIN = 40
CLI_WIDTH_MAX = 120


def clamp_int(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def get_terminal_width(default: int = 60) -> int:
    """
    Get current terminal width in characters.
    Clamped for nicer CLI output.
    """
    try:
        size = shutil.get_terminal_size(fallback=(default, 24))
        columns = int(getattr(size, "columns", default) or default)
        # a small margin so wrapping doesn't "hug" the edge
        columns = columns - 2
        return clamp_int(columns, CLI_WIDTH_MIN, CLI_WIDTH_MAX)
    except Exception:
        return clamp_int(default, CLI_WIDTH_MIN, CLI_WIDTH_MAX)


def clear_screen() -> None:
    """
    Clear the terminal screen (cross-platform).
    Tries ANSI first (fast), falls back to system commands, then prints newlines.
    """
    try:
        if sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
            # ANSI clear screen + move cursor home
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            return
    except Exception:
        pass

    try:
        command = "cls" if platform.system().lower().startswith("win") else "clear"
        os.system(command)
    except Exception:
        print("\n" * 50)


def pause(message: str = "Tekan [Enter] untuk melanjutkan...") -> None:
    """Pause program execution until Enter is pressed."""
    try:
        print("")
        input(message)
    except (KeyboardInterrupt, EOFError):
        # Silent exit from pause
        return


# =============================================================================
# FORMATTING UTILITIES
# =============================================================================

_BytesLike = Union[int, float, str, None]


def format_bytes(size: _BytesLike, *, precision: int = 2) -> str:
    """
    Format a byte count into a human-readable string using binary units.
    Examples:
      1073741824 -> '1.00 GiB'
      '1,024'    -> '1.00 KiB'

    Notes:
    - Returns '0 B' for None/invalid/<=0.
    - Uses IEC units: KiB, MiB, GiB...
    """
    if size is None:
        return "0 B"

    try:
        if isinstance(size, str):
            # allow "1,024" / "1_024" / "  1024 "
            s = size.strip().replace(",", "").replace("_", "")
            if not s:
                return "0 B"
            value = float(s)
        else:
            value = float(size)

        if not (value > 0):
            return "0 B"

        units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
        step = 1024.0
        idx = 0
        while value >= step and idx < (len(units) - 1):
            value /= step
            idx += 1

        if idx == 0:
            # bytes should not show decimals
            return f"{int(round(value))} {units[idx]}"

        return f"{value:.{precision}f} {units[idx]}"
    except (ValueError, TypeError):
        return "0 B"


# Backwards-compatible alias (if other code imports the old name)
format_quota_byte = format_bytes


# =============================================================================
# HTML PARSING UTILITIES
# =============================================================================

class HTMLToText(HTMLParser):
    """
    A small HTML-to-text converter suited for CLI descriptions.

    Strategy:
    - Convert common block tags to newlines
    - Convert <li> to bullet lines
    - Normalize whitespace at the end
    """

    _BLOCK_TAGS = {"p", "div", "section", "article", "header", "footer", "br", "hr"}
    _LIST_ITEM_TAGS = {"li"}

    def __init__(self) -> None:
        # convert_charrefs=True in Py3 HTMLParser by default; keeps entities clean.
        super().__init__()
        self._parts: list[str] = []
        self._just_added_newline = False

    def _add(self, text: str) -> None:
        if not text:
            return
        self._parts.append(text)

    def _newline(self) -> None:
        # Avoid stacking too many newlines in a row
        if not self._just_added_newline:
            self._parts.append("\n")
            self._just_added_newline = True

    def handle_starttag(self, tag: str, attrs) -> None:
        t = tag.lower()
        if t in self._BLOCK_TAGS:
            self._newline()
        elif t in self._LIST_ITEM_TAGS:
            self._newline()
            self._add("• ")
            self._just_added_newline = False

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t in {"p", "div", "section", "article"}:
            self._newline()

    def handle_data(self, data: str) -> None:
        txt = data.replace("\xa0", " ")
        txt = _html.unescape(txt)
        # Keep words separated, but don't aggressively strip structure.
        txt = re.sub(r"[ \t]+", " ", txt)
        txt = txt.strip()
        if txt:
            self._add(txt + " ")
            self._just_added_newline = False

    def get_clean_text(self) -> str:
        raw = "".join(self._parts)

        # Normalize line endings
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")

        # Remove space before newlines, collapse multiple spaces
        raw = re.sub(r"[ \t]+\n", "\n", raw)
        raw = re.sub(r"[ \t]{2,}", " ", raw)

        # Collapse 3+ newlines into 2 (paragraph separation)
        raw = re.sub(r"\n{3,}", "\n\n", raw)

        # Trim each line, keep paragraph breaks
        lines = [ln.strip() for ln in raw.split("\n")]
        cleaned = "\n".join(lines)

        return cleaned.strip()


def _wrap_cli_text(text: str, width: int) -> str:
    """
    Wrap text for CLI with nicer handling for bullet lines.
    """
    width = clamp_int(int(width), CLI_WIDTH_MIN, CLI_WIDTH_MAX)

    out_lines: list[str] = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            out_lines.append("")
            continue

        stripped = line.lstrip()
        if stripped.startswith("• "):
            bullet_content = stripped[2:].strip()
            wrapper = textwrap.TextWrapper(
                width=width,
                initial_indent="• ",
                subsequent_indent="  ",
                break_long_words=False,
                break_on_hyphens=False,
            )
            out_lines.extend(wrapper.wrap(bullet_content) or ["•"])
        else:
            wrapper = textwrap.TextWrapper(
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
            out_lines.extend(wrapper.wrap(stripped) or [stripped])

    # Remove accidental multiple blank lines from wrapping process
    result = "\n".join(out_lines)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result


def strip_html(raw_html: str) -> str:
    """
    Very simple HTML tag stripping fallback (regex-based).
    Prefer display_html() for nicer formatting.
    """
    if not raw_html:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = _html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def display_html(raw_html: str, width: int = 0) -> str:
    """
    Convert HTML into CLI-friendly wrapped text.
    """
    if not raw_html:
        return "Tidak ada deskripsi."

    if width <= 0:
        width = get_terminal_width()

    try:
        parser = HTMLToText()
        parser.feed(raw_html)
        parser.close()
        text = parser.get_clean_text()
        if not text:
            return "Tidak ada deskripsi."
        return _wrap_cli_text(text, width)
    except Exception as e:
        # Log in a library-friendly way (won't print unless app configures logging)
        logger.debug("Error parsing HTML: %s", e, exc_info=True)
        # Fallback: regex strip
        text = strip_html(raw_html)
        return _wrap_cli_text(text or "Tidak ada deskripsi.", width)


__all__ = [
    "try_load_dotenv",
    "configure_default_logging",
    "get_terminal_width",
    "clear_screen",
    "pause",
    "format_bytes",
    "format_quota_byte",
    "HTMLToText",
    "strip_html",
    "display_html",
]