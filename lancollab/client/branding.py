"""Local terminal wordmark; no network or extra font dependency."""

import os
import shutil
import sys

BANNER = (
    "   __    _   _  __  __  _   __   __    _   ___ ",
    "  / /  .' \\ / |/ /,'_/,' \\ / /  / /  .' \\ / o.)",
    " / /_ / o // || // /_/ o |/ /_ / /_ / o // o \\ ",
    "/___//_n_//_/|_/ |__/|_,'/___//___//_n_//___,' ",
)


def banner_lines(width=80):
    if width >= max(map(len, BANNER)):
        return BANNER
    return tuple(
        line[: max(0, width)]
        for line in (
            "╭────────────────────────────╮",
            "│   L A N - C O L L A B       │",
            "╰────────────────────────────╯",
            "Connect. Chat. Create together.",
        )
    )


def print_welcome(stream=None):
    stream = stream if stream is not None else sys.stdout
    if not stream.isatty():
        return
    color = "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    accent, reset = ("\033[1;36m", "\033[0m") if color else ("", "")
    width = shutil.get_terminal_size(fallback=(80, 24)).columns
    art = "\n".join(banner_lines(width))
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        art.encode(encoding)
    except UnicodeEncodeError:
        art = "LAN-COLLAB\nConnect. Chat. Create together."
    print("\n" + accent + art + reset, file=stream)
    print("\n  Your people. Your files. Your local network.\n", file=stream)
