"""The help screen renders the shared command reference and fits the terminal."""

import unittest

from lancollab.client.cli import _DETAILED_HELP
from lancollab.client.tui import cell_width, help_lines


class HelpLinesTests(unittest.TestCase):
    def test_lists_every_reference_line(self):
        # Wrapping only breaks lines; removing spaces re-joins them exactly,
        # so every reference line must survive verbatim.
        squashed = "".join(text for text, _ in help_lines(76, role="admin")).replace(" ", "")
        for line in _DETAILED_HELP:
            if line.strip():
                self.assertIn(line.replace(" ", ""), squashed)

    def test_members_only_see_commands_they_can_use(self):
        member = " ".join(text for text, _ in help_lines(76))
        admin = " ".join(text for text, _ in help_lines(76, role="admin"))
        self.assertNotIn("/approve", member)
        self.assertNotIn("/reject-user", member)
        self.assertIn("/approve", admin)
        self.assertIn("/status", member)

    def test_no_row_is_wider_than_the_screen(self):
        for width in (76, 50, 38):
            for text, _ in help_lines(width):
                self.assertLessEqual(cell_width(text), width)

    def test_title_sections_and_return_hint(self):
        rows = help_lines(76)
        self.assertEqual(rows[0][0], "LAN COLLAB — Help")
        joined = " ".join(text for text, _ in rows)
        for section in ("CHAT AND PEOPLE", "FILES", "CONNECTION", "HELP AND EXIT"):
            self.assertIn(section, joined)
        self.assertIn("F1 or /help opens this help as a separate screen", joined)
        self.assertIn("Ctrl+C / Esc — back to chat", joined)

    def test_narrow_screen_still_wraps_cleanly(self):
        rows = help_lines(38)
        self.assertGreater(len(rows), len(_DETAILED_HELP))  # Long lines wrapped.
        self.assertEqual(rows[0][0], "LAN COLLAB — Help")


if __name__ == "__main__":
    unittest.main()
