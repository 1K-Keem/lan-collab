"""Welcome banner: the LAN logo only, no mascot.

The penguin that used to sit next to the name read as a padlock at
terminal size, so it was removed (2026-11-11).
"""

import unittest

from lancollab.client.branding import BANNER, banner_lines


class BrandingTests(unittest.TestCase):
    def test_wide_terminal_shows_logo(self):
        lines = banner_lines(80)
        self.assertEqual(lines, BANNER)
        self.assertTrue(all(line.isascii() for line in lines))
        self.assertIn("C O L L A B", "\n".join(lines))

    def test_medium_terminal_shows_logo_only(self):
        self.assertEqual(banner_lines(40), BANNER)

    def test_narrow_terminal_shows_plain_fallback(self):
        lines = banner_lines(30)
        self.assertIn("LAN COLLAB", lines[0])


if __name__ == "__main__":
    unittest.main()
