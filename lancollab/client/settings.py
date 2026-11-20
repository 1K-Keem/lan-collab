"""Small per-user display preferences, kept next to the token store."""

import json
import os

from lancollab.client.token_store import _config_dir

THEMES = ("dark", "light")


def _settings_file():
    return _config_dir() / "settings.json"


def load_theme():
    """LANCOLLAB_THEME wins, then the saved choice, then dark."""
    chosen = os.environ.get("LANCOLLAB_THEME", "").strip().lower()
    if chosen in THEMES:
        return chosen
    try:
        saved = json.loads(_settings_file().read_text(encoding="utf-8")).get("theme")
    except (OSError, ValueError, AttributeError, RuntimeError):
        return "dark"
    return saved if saved in THEMES else "dark"


def save_theme(name):
    """Remember the theme; an unwritable config folder only loses the preference."""
    if name not in THEMES:
        raise ValueError("Theme must be dark or light.")
    try:
        path = _settings_file()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data["theme"] = name
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except (OSError, RuntimeError):
        return False
    return True
