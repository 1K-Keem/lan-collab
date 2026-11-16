"""Clipboard providers are mocked: tests never change the user's clipboard."""

import subprocess
import unittest
from unittest.mock import patch

from lancollab.client.clipboard import ClipboardError, copy_text


class ClipboardTests(unittest.TestCase):
    def invoke(self, platform, environment, text, *, runner=None, available=None):
        with (
            patch("lancollab.client.clipboard.sys.platform", platform),
            patch.dict("os.environ", environment, clear=True),
            patch(
                "lancollab.client.clipboard.shutil.which",
                side_effect=available or (lambda name: "/tools/" + name),
            ),
            patch("lancollab.client.clipboard.subprocess.run", side_effect=runner) as run,
        ):
            copy_text(text)
        return run

    def test_exact_utf8_body_sent_on_stdin_for_each_platform(self):
        body = "  Xin chào 世界\nsecond line\t$(echo nope); `quote`\n"
        for platform, environment, executable in (
            ("win32", {}, "powershell.exe"),
            ("linux", {"WSL_DISTRO_NAME": "Ubuntu"}, "powershell.exe"),
            ("darwin", {}, "pbcopy"),
            ("linux", {"WAYLAND_DISPLAY": "wayland-0"}, "wl-copy"),
            ("linux", {"DISPLAY": ":0"}, "xclip"),
        ):
            with self.subTest(platform=platform, environment=environment):
                run = self.invoke(platform, environment, body)
                args, kwargs = run.call_args
                self.assertEqual(args[0][0], "/tools/" + executable)
                self.assertEqual(kwargs["input"], body.encode("utf-8"))
                self.assertNotIn(body, args[0])
                self.assertNotIn("shell", kwargs)
                self.assertEqual(kwargs["timeout"], 3)

    def test_failed_provider_falls_back(self):
        run = self.invoke(
            "linux",
            {"DISPLAY": ":0"},
            "hello",
            runner=[subprocess.TimeoutExpired("xclip", 3), None],
        )
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0][0], "/tools/xsel")

    def test_errors_are_actionable_and_not_success(self):
        with self.assertRaisesRegex(ClipboardError, "Could not access"):
            self.invoke("darwin", {}, "hello", runner=OSError("unavailable"))
        with self.assertRaisesRegex(ClipboardError, "Clipboard unavailable"):
            self.invoke("linux", {}, "hello")
        with self.assertRaisesRegex(ClipboardError, "null character"):
            self.invoke("win32", {}, "hello\x00there")
