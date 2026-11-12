"""Install the client command for this OS user without touching system Python."""

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[1]


def installer_home():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "lan-collab" / "setup-tools"


def run(command):
    subprocess.run([str(part) for part in command], check=True)


def pipx_command():
    executable = shutil.which("pipx")
    if executable:
        return [executable]
    if importlib.util.find_spec("pipx") is not None:
        return [sys.executable, "-m", "pipx"]
    # Bootstrap privately: works even in a project venv or with OS-managed Python.
    directory = installer_home()
    python = directory / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if (
        not python.exists()
        or subprocess.run([str(python), "-m", "pip", "--version"], capture_output=True).returncode
    ):
        print("Preparing setup tools…", flush=True)
        try:
            venv.EnvBuilder(with_pip=True).create(directory)
        except (OSError, subprocess.CalledProcessError) as error:
            raise RuntimeError(
                "Could not create the setup environment. Install Python's venv support "
                "(python3-venv on Debian/Ubuntu), then run setup again."
            ) from error
    available = subprocess.run([str(python), "-c", "import pipx"], capture_output=True)
    if available.returncode:
        run([python, "-m", "pip", "install", "pipx"])
    return [str(python), "-m", "pipx"]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install or update the lancollab command")
    parser.add_argument("--uninstall", action="store_true", help="Remove the installed application")
    parser.add_argument("--no-path", action="store_true", help="Do not update your shell PATH")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print("Setup requires Python 3.11 or newer.", file=sys.stderr)
        return 1
    if not args.uninstall and os.name != "nt" and importlib.util.find_spec("curses") is None:
        print(
            "This Python lacks curses support. Install Python with curses and retry.",
            file=sys.stderr,
        )
        return 1
    try:
        pipx = pipx_command()
        if args.uninstall:
            run([*pipx, "uninstall", "lancollab"])
            print("Removed lancollab. Saved history, login, and downloaded files were kept.")
            return 0
        print("Installing LAN Collab…", flush=True)
        # Use the base interpreter, so removing a project .venv cannot break the app.
        python = getattr(sys, "_base_executable", None) or sys.executable
        run([*pipx, "install", "--force", "--python", python, ROOT])
        if not args.no_path:
            run([*pipx, "ensurepath"])
        print("\nInstalled. Open a new terminal and type: lancollab")
        print("Default server: 206.189.94.234:40000")
        print("Run setup again after updating the project to update the installed app.")
        if args.no_path:
            print("PATH was not changed; the pipx command directory must already be on PATH.")
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Setup failed: {error}", file=sys.stderr)
        print(
            "Check the error above and your network connection, then run setup again.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
