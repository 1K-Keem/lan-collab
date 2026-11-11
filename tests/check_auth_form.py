"""Run: python -m tests.check_auth_form (no native curses needed)."""
import sys
from unittest.mock import patch
from tests.test_tui_context_menu import FakeWindow, fake_curses, click, expect
from lancollab.client.auth_tui import prompt_auth


def check(events, **kwargs):
    window = FakeWindow(26, 90, events)
    module = fake_curses(window)
    window.module = module
    with patch.dict(sys.modules, {"curses": module}):
        result = prompt_auth("127.0.0.1:40000", **kwargs)
    return result, window


def main():
    result, _ = check([
        "alice", "\t", "secret123",
        expect(lambda w: "secret123" not in w.all_text() and "*********" in w.all_text(), "\t"),
        " ", "\t", "\n",
    ])
    assert result == ("login", "alice", "secret123", True)
    result, _ = check([
        click("[ Sign up ]"), "bob", "\t", "password123", "\t", "different", "\t", "\n",
        expect(lambda w: "Passwords do not match" in w.all_text(), "\x15"),
        "password123", "\t", "\n",
    ])
    assert result == ("signup", "bob", "password123", False)
    result, _ = check([
        expect(lambda w: "Wrong password" in w.all_text() and "alice" in w.all_text(), "\x1b"),
    ], message="Wrong password", username="alice")
    assert result is None
    result, _ = check([lambda w: w.module.KEY_F2, "carol", "\t", "password123", "\t", "password123", "\t", "\n"])
    assert result == ("signup", "carol", "password123", False)
    print("Authentication form checks passed: masking, mouse/keyboard, mismatch correction, inline retry, cancel")


if __name__ == "__main__":
    main()
