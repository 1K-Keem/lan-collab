"""Setup command sequencing, error propagation, and update/uninstall scope."""

import contextlib
import io
import subprocess
import unittest
from unittest.mock import patch

from scripts import setup_client


class SetupClientTests(unittest.TestCase):
    def invoke(self, args, runner):
        with (
            patch.object(
                setup_client, "pipx_command", return_value=["private-python", "-m", "pipx"]
            ),
            patch.object(setup_client, "run", runner),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return setup_client.main(args)

    def test_install_and_update_use_exact_repository_path_then_ensurepath(self):
        calls = []
        self.assertEqual(self.invoke([], calls.append), 0)
        self.assertEqual(calls[0][:5], ["private-python", "-m", "pipx", "install", "--force"])
        self.assertEqual(calls[0][-1], setup_client.ROOT)
        self.assertIn("--python", calls[0])
        self.assertEqual(calls[1], ["private-python", "-m", "pipx", "ensurepath"])
        self.assertEqual(len(calls), 2)

    def test_no_path_skips_shell_changes(self):
        calls = []
        self.assertEqual(self.invoke(["--no-path"], calls.append), 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("install", calls[0])

    def test_uninstall_only_removes_named_application(self):
        calls = []
        self.assertEqual(self.invoke(["--uninstall"], calls.append), 0)
        self.assertEqual(calls, [["private-python", "-m", "pipx", "uninstall", "lancollab"]])

    def test_failed_install_does_not_report_success_or_modify_path(self):
        calls = []

        def fail(command):
            calls.append(command)
            raise subprocess.CalledProcessError(1, command)

        self.assertEqual(self.invoke([], fail), 1)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
