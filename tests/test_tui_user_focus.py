"""User-facing screens keep useful state without diagnostic noise."""

import unittest

from tests.test_tui_context_menu import FakeSession, click, expect, run


class UserFocusTests(unittest.TestCase):
    def test_header_is_simple_in_every_chat_tab(self):
        session = FakeSession(users=("bob", "carol"))

        def clean(window):
            header = window.lines.get(0, "")
            return "LAN-COLLAB · alice · 2 online" in header and all(
                word not in window.all_text()
                for word in (
                    "TCP",
                    "UDP",
                    "TLS",
                    "Live conversation",
                    "Scrollback",
                    "reply:",
                    "No active transfers",
                    "PENDING (0)",
                )
            )

        run(
            session,
            [
                expect(clean, click("2:Global")),
                expect(clean, click("3:Direct")),
                expect(clean, click("4:Files")),
                expect(clean, "\x03"),
            ],
        )

    def test_background_refresh_is_quiet_but_errors_are_visible(self):
        session = FakeSession()

        def notices(window):
            session.output("[auth] Authenticated as member")
            session.output("[presence] Online list refreshed")
            session.output("[error] Could not save local chat history")
            return ""

        run(
            session,
            [
                notices,
                expect(
                    lambda w: (
                        "Online list refreshed" not in w.all_text()
                        and "Authenticated as" not in w.all_text()
                        and "Could not save local chat history" in w.all_text()
                    ),
                    "\x03",
                ),
            ],
        )

    def test_unread_indicator_only_counts_chat_while_scrolled(self):
        session = FakeSession()

        def seed(window):
            for index in range(30):
                session.output(f"[All] bob: message {index}")
            return ""

        def incoming(window):
            session.output("[presence] Online list refreshed")
            session.output("[All] bob: new message")
            return ""

        run(
            session,
            [
                seed,
                lambda w: w.module.KEY_PPAGE,
                incoming,
                expect(lambda w: "↓ 1 new message" in w.all_text(), lambda w: w.module.KEY_NPAGE),
                expect(lambda w: "↓ 1 new message" not in w.all_text(), "\x03"),
            ],
        )

    def test_file_summary_hides_ids_and_preserves_verification_state(self):
        session = FakeSession(
            transfers=[
                dict(
                    id="private-transfer-id",
                    name="notes.pdf",
                    peer="bob",
                    direction="out",
                    status="verifying",
                    done=10,
                    size=10,
                )
            ]
        )
        run(
            session,
            [
                expect(
                    lambda w: (
                        "Checking file" in w.all_text()
                        and "notes.pdf" in w.all_text()
                        and "private-" not in w.all_text()
                    ),
                    "\x03",
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()
