"""Remember-me token storage on the client machine.

One token per server host. Backend, in order:
1. the ``keyring`` package, if installed -- OS credential store
   (Windows Credential Manager / macOS Keychain / Linux Secret Service);
2. otherwise a file with mode 0600 in the user config dir
   (``~/.config/lan-collab/tokens.json`` on Linux, ``%APPDATA%`` on
   Windows). Never a file in the project folder, never world-readable.

The raw token is stored here; the server stores only its SHA-256 hash.
Swapping the backend is a change to this file only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

SERVICE = "lan-collab"


def _config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    directory = base / "lan-collab"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


class TokenStore:
    """Save / load / delete one (username, token) pair per server host."""

    def __init__(self, directory: Path | None = None) -> None:
        self._directory = directory or _config_dir()
        self._file = self._directory / "tokens.json"
        try:
            import keyring  # type: ignore[import-not-found]

            self._keyring = keyring
            self.backend = "keyring"
        except ImportError:
            self._keyring = None
            self.backend = "file-0600"

    # -- file backend ------------------------------------------------------
    def _file_read(self) -> dict:
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _file_write(self, data: dict) -> None:
        self._file.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(self._file, 0o600)

    # -- public API --------------------------------------------------------
    def save(self, host: str, username: str, token: str) -> None:
        if self._keyring is not None:
            self._keyring.set_password(
                SERVICE, host, json.dumps({"username": username, "token": token})
            )
            return
        data = self._file_read()
        data[host] = {"username": username, "token": token}
        self._file_write(data)

    def load(self, host: str) -> tuple[str, str] | None:
        if self._keyring is not None:
            try:
                raw = self._keyring.get_password(SERVICE, host)
            except Exception:
                return None
            if not raw:
                return None
            try:
                entry = json.loads(raw)
                return entry["username"], entry["token"]
            except (ValueError, KeyError):
                return None
        entry = self._file_read().get(host)
        if not entry:
            return None
        return entry.get("username"), entry.get("token")

    def delete(self, host: str) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, host)
            except Exception:
                pass
            return
        data = self._file_read()
        data.pop(host, None)
        self._file_write(data)
