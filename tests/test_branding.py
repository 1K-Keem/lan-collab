"""Full wordmark and compact terminal branding."""

import unittest

from lancollab.client.branding import BANNER, banner_lines


class BrandingTests(unittest.TestCase):
    def test_wide_terminal_shows_logo(self):
        lines = banner_lines(80)
        self.assertEqual(lines, BANNER)
        self.assertTrue(all(line.isascii() for line in lines))
        self.assertIn("/___//_n_//_/|_/", lines[3])

    def test_medium_terminal_shows_logo_only(self):
        self.assertIn("L A N - C O L L A B", "\n".join(banner_lines(40)))

    def test_narrow_terminal_shows_plain_fallback(self):
        lines = banner_lines(30)
        self.assertIn("L A N - C O L L A B", "\n".join(lines))
        for width in (10, 30, 40, 59, 80):
            self.assertTrue(all(len(line) <= width for line in banner_lines(width)))


if __name__ == "__main__":
    unittest.main()
