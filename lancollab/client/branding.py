"""Original, local ASCII welcome artwork; no network or extra font dependency."""

import os
import shutil
import sys

BANNER = (
    r"  _        _    _   _    [o]---[o]",
    r" | |      / \  | \ | |     \ /",
    r" | |___  / _ \ |  \| |     [o]",
    r" |_____|/_/ \_\|_|\__|  C O L L A B",
)


def banner_lines(width=80):
    # No mascot: the penguin that used to stand next to the name read as a
    # padlock at terminal size, so the banner is the logo only.
    if width >= 38:
        return BANNER
    return ("[o]---[o]  LAN COLLAB", "   \\ /     Your study space", "   [o]")


def print_welcome(stream=None):
    stream = stream if stream is not None else sys.stdout
    if not stream.isatty():
        return
    color = "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    accent, reset = ("\033[1;36m", "\033[0m") if color else ("", "")
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    print("\n" + accent + "\n".join(banner_lines(width)) + reset, file=stream)
    print("\n  Your people. Your files. Your local network.\n", file=stream)
