"""Installed command: open the TUI with the group's default server."""

import sys

from lancollab.client.__main__ import main as client_main

DEFAULT_HOST = "206.189.94.234"
DEFAULT_PORT = "40000"


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    # Explicit discovery should not be masked by the installed default host.
    defaults = ["--port", DEFAULT_PORT, "--tui"]
    if "--discover" not in args:
        defaults += ["--host", DEFAULT_HOST]
    return client_main([*defaults, *args])


if __name__ == "__main__":
    raise SystemExit(main())
