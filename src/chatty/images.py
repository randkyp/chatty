"""
Image utilities for multimodal support.

Handles base64 encoding, extracting image paths from user text,
and retrieving images from the macOS or Linux system clipboard.
"""

from __future__ import annotations

import base64
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# Regular expression to match @/path or @"/path" or @'/path'
IMAGE_PATH_RE = re.compile(
    r'@(?:(["\'])([^"\']+)\1|((?:[^\s"\'()\[\]{}*?<>|\\]|\\.)+))'
)


def encode_image(image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Encode raw image bytes into a standard base64 data URL."""
    encoded = base64.b64encode(image_bytes).decode("utf-8")
    return f"data:{mime_type};base64,{encoded}"


def encode_image_file(path: Path) -> tuple[str, str] | None:
    """Read a local file, guess its mime type, and return (data_url, mime_type)."""
    if not path.exists() or not path.is_file():
        return None
    mime_type, _ = mimetypes.guess_type(path)
    if not mime_type or not mime_type.startswith("image/"):
        return None
    try:
        data = path.read_bytes()
        return encode_image(data, mime_type), mime_type
    except Exception:
        return None


def extract_images_from_text(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Scan text for @path pointing to existing image files.

    Returns (cleaned_text, images) where successfully resolved paths are removed
    from the text, and images is a list of dictionaries:
        {"data_url": str, "path": str, "mime_type": str}
    Duplicates by resolved path are excluded.
    """
    images: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    def replacer(match: re.Match[str]) -> str:
        path_str = (match.group(2) or match.group(3) or "").strip()
        try:
            path_str = re.sub(r'\\(.)', r'\1', path_str)
            path = Path(path_str).expanduser().resolve()
            if path.exists() and path.is_file():
                resolved_str = str(path)
                if resolved_str not in seen_paths:
                    seen_paths.add(resolved_str)
                    res = encode_image_file(path)
                    if res:
                        data_url, mime = res
                        images.append({
                            "data_url": data_url,
                            "path": resolved_str,
                            "mime_type": mime,
                        })
                # If it's a valid file, remove it from the text
                return ""
        except Exception:
            pass
        # Not a valid file, leave the text as is
        return match.group(0)

    cleaned_text = IMAGE_PATH_RE.sub(replacer, text)
    # Return cleaned text with extra spaces removed
    return cleaned_text.strip(), images


def get_clipboard_image() -> bytes | None:
    """Retrieve raw image bytes from the system clipboard.

    Supports macOS (via AppleScript) and Linux (via wl-paste or xclip).
    Returns None if no image is in the clipboard or clipboard tools are missing.
    """
    if sys.platform == "darwin":
        return _get_clipboard_image_macos()
    elif sys.platform.startswith("linux"):
        return _get_clipboard_image_linux()
    return None


def _get_clipboard_image_macos() -> bytes | None:
    """Extract PNG or TIFF image from macOS clipboard using osascript."""
    temp_dir = Path(tempfile.gettempdir())
    temp_file = temp_dir / "chatty_clipboard.png"
    if temp_file.exists():
        try:
            temp_file.unlink()
        except Exception:
            pass

    # AppleScript that attempts to retrieve PNG first, falling back to TIFF
    script = f'''
    try
        set theFile to POSIX file "{temp_file}"
        set theImage to the clipboard as «class PNGf»
        set f to open for access theFile with write permission
        set eof f to 0
        write theImage to f
        close access f
        return "PNG"
    on error
        try
            set theFile to POSIX file "{temp_file}"
            set theImage to the clipboard as «class TIFF»
            set f to open for access theFile with write permission
            set eof f to 0
            write theImage to f
            close access f
            return "TIFF"
        on error
            return "NONE"
        end try
    end try
    '''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
        result = proc.stdout.strip()
        if result in ("PNG", "TIFF") and temp_file.exists():
            if result == "TIFF":
                png_file = temp_file.with_suffix(".png")
                sips_proc = subprocess.run(
                    ["sips", "-s", "format", "png", str(temp_file), "--out", str(png_file)],
                    capture_output=True,
                )
                if sips_proc.returncode == 0 and png_file.exists():
                    data = png_file.read_bytes()
                    png_file.unlink()
                else:
                    data = None
            else:
                data = temp_file.read_bytes()
            try:
                temp_file.unlink()
            except Exception:
                pass
            return data
    except Exception:
        pass
    return None


def _get_clipboard_image_linux() -> bytes | None:
    """Extract PNG image from Linux clipboard using wl-paste or xclip."""
    # 1. Try Wayland wl-paste
    if shutil.which("wl-paste"):
        try:
            proc = subprocess.run(
                ["wl-paste", "-t", "image/png"],
                capture_output=True,
                timeout=5.0,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            pass

    # 2. Try X11 xclip
    if shutil.which("xclip"):
        try:
            proc = subprocess.run(
                ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                capture_output=True,
                timeout=5.0,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            pass

    return None
