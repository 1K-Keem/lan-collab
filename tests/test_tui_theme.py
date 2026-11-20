"""Dark/light themes, unread tab badges and word-aware wrapping."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lancollab.client import settings
from lancollab.client.tui import cell_width, suggestions, wrap_words
from tests.test_tui_context_menu import FakeSession, click, expect, run


class ThemeSettingsTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        patcher = patch.object(settings, "_settings_file", return_value=self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_saved_choice_and_environment_override(self):
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(settings.load_theme(), "dark")
            self.assertTrue(settings.save_theme("light"))
            self.assertEqual(settings.load_theme(), "light")
        with patch.dict("os.environ", {"LANCOLLAB_THEME": "dark"}):
            self.assertEqual(settings.load_theme(), "dark")
        with self.assertRaises(ValueError):
            settings.save_theme("purple")

    def test_corrupt_settings_fall_back_to_dark(self):
        self.path.write_text("not json", encoding="utf-8")
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(settings.load_theme(), "dark")

    def test_theme_command_switches_saves_and_never_reaches_the_server(self):
        session = FakeSession()
        with patch.dict("os.environ", {}, clear=True):
            run(
                session,
                [
                    "/theme light\n",
                    expect(lambda w: "light theme" in w.all_text(), "/theme\n"),
                    expect(lambda w: "dark theme" in w.all_text(), "/theme blue\n"),
                    expect(lambda w: "Usage: /theme" in w.all_text(), "\x03"),
                ],
            )
            self.assertEqual(settings.load_theme(), "dark")
        self.assertEqual(session.executed, [])

    def test_theme_completion(self):
        self.assertIn("/theme", [s.value for s in suggestions("/th")])
        self.assertEqual([s.value for s in suggestions("/theme l")], ["/theme light"])


class UnreadBadgeTests(unittest.TestCase):
    def test_messages_in_hidden_tabs_are_counted_until_opened(self):
        session = FakeSession()

        def arrive(window):
            session.output("[DM] bob: psst")
            session.output("[All] carol: hi all")
            return window.module.KEY_RESIZE

        run(
            session,
            [
                click("Global"),
                arrive,
                expect(
                    lambda w: "Direct 1" in w.lines[1] and "Global 1" not in w.lines[1],
                    click("Direct"),
                ),
                expect(lambda w: "Direct 1" not in w.lines[1], "\x03"),
            ],
        )


class WrapWordsTests(unittest.TestCase):
    def test_breaks_at_spaces_and_splits_only_long_words(self):
        self.assertEqual(wrap_words("hello brave new world", 11), ["hello brave", "new world"])
        rows = wrap_words("a" * 25, 10)
        self.assertEqual(rows, ["a" * 10, "a" * 10, "a" * 5])
        for width in (2, 3, 8):
            for row in wrap_words("你好 world é xin chào", width):
                self.assertLessEqual(cell_width(row), width)
        self.assertEqual("".join(wrap_words("你好", 1)), "你好")  # Never loops or drops text.


if __name__ == "__main__":
    unittest.main()
