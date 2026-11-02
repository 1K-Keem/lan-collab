"""Receiver-side filename sanitization (protocol-spec section 4, "Filename and file integrity").

The wire filename is untrusted. The receiver derives a safe local basename from it and never
uses sender-supplied directory components.
"""

from __future__ import annotations

import re
import unicodedata

WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_ILLEGAL = frozenset('<>:"/\\|?*')
# Leave room for a " (NN)" uniqueness suffix inside the 255-byte filesystem limit.
MAX_BASENAME_BYTES = 240
_SEPARATORS = re.compile(r"[\\/]")


def _truncate(name: str, limit: int) -> str:
    if len(name.encode("utf-8")) <= limit:
        return name
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem or len(ext.encode("utf-8")) > 20:
        stem, ext = name, ""
    else:
        ext = "." + ext
    budget = limit - len(ext.encode("utf-8"))
    out = ""
    for ch in stem:
        if len((out + ch).encode("utf-8")) > budget:
            break
        out += ch
    return (out.rstrip(" .") or "file") + ext


def sanitize_filename(name: str) -> str:
    """Return a safe, non-empty basename valid on Windows, macOS and Linux."""
    name = name.encode("utf-8", "replace").decode("utf-8")  # lone surrogates -> "?"
    name = unicodedata.normalize("NFC", name)
    name = _SEPARATORS.split(name)[-1]
    name = "".join(
        "_" if ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _ILLEGAL else ch for ch in name
    )
    name = name.strip(" .")  # no leading/trailing dots or spaces ("..", hidden files, Windows)
    if not name:
        return "file"
    if name.split(".", 1)[0].rstrip(" ").upper() in WINDOWS_RESERVED:
        name = "_" + name
    return _truncate(name, MAX_BASENAME_BYTES)
