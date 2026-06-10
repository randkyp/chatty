"""
Clipboard helpers.

Text copy goes through ``pyperclip`` (a hard dependency), which already handles
macOS (pbcopy), Linux (xclip/xsel/wl-clipboard) and Windows (clip). Image *paste*
from the clipboard lives in ``images.py`` since it needs format conversion.
"""

from __future__ import annotations

import pyperclip


class ClipboardError(Exception):
    """Raised when copying to the clipboard fails."""


def copy_text(text: str) -> None:
    """Copy *text* to the system clipboard.

    Raises ClipboardError if no clipboard mechanism is available.
    """
    try:
        pyperclip.copy(text)
    except pyperclip.PyperclipException as e:  # pragma: no cover - platform dependent
        raise ClipboardError(str(e)) from e
