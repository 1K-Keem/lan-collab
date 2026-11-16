"""Copy explicit user-selected text through a local clipboard provider."""

import os
import shutil
import subprocess
import sys


class ClipboardError(RuntimeError):
    pass


_POWERSHELL_COPY = (
    "$ErrorActionPreference='Stop'; "
    "[Console]::InputEncoding = New-Object System.Text.UTF8Encoding; "
    "$text = [Console]::In.ReadToEnd(); Set-Clipboard -Value $text"
)


def copy_text(text):
    """Send content on stdin, never interpolate a message into executable code."""
    if "\x00" in text:
        raise ClipboardError("This message contains a null character and cannot be copied intact.")
    commands = []
    if (
        sys.platform == "win32"
        or os.environ.get("WSL_DISTRO_NAME")
        or os.environ.get("WSL_INTEROP")
    ):
        commands.append(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_COPY]
        )
    if sys.platform == "darwin":
        commands.append(["pbcopy"])
    if os.environ.get("WAYLAND_DISPLAY"):
        commands.append(["wl-copy"])
    if os.environ.get("DISPLAY"):
        commands.extend((["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]))
    attempted = False
    for command in commands:
        executable = shutil.which(command[0])
        if not executable:
            continue
        attempted = True
        try:
            subprocess.run(
                [executable, *command[1:]],
                input=text.encode("utf-8"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=3,
            )
            return
        except (OSError, subprocess.SubprocessError):
            continue
    if attempted:
        raise ClipboardError("Could not access the clipboard. Try again or use terminal selection.")
    raise ClipboardError(
        "Clipboard unavailable. Use terminal selection, or install wl-clipboard (Wayland) / xclip (X11)."
    )
