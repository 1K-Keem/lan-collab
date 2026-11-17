"""Group persistence, authorization, protocol and terminal routing checks."""

from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
import sqlite3
import unittest
from unittest.mock import patch

from lancollab.client.application import ClientState, CollabClient
from lancollab.client.cli import ConsoleSession, parse_command
from lancollab.client.history import HistoryStore
from lancollab.client.transfers import TransferError
from lancollab.common.codec import JsonFrameCodec, OutboundMessageError
from lancollab.common.contracts import Envelope, SessionIdentity
from lancollab.server.accounts import AccountError, AccountService
from lancollab.server.application import CollabServer
from tests.fake_transport import FakeTransport
from tests.test_tui_context_menu import FakeSession, click, expect, run


class GroupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "accounts.db"
        self.accounts = AccountService(self.db)
        self.addCleanup(self.accounts.close)
        self.server = CollabServer(self.accounts)
        self.identities = {}
        for name in ("alice", "bob", "eve"):
            self.accounts.create_account(name, "synthetic-password", status="active")
            identity = SessionIdentity("127.0.0.1", 10000, name)
            self.identities[name] = identity
            self.server.handle_message(
                Envelope("AUTH_REQUEST", f"auth-{name}", {
                    "username": name, "password": "synthetic-password",
                }), identity,
            )

    def request(self, name, kind, payload):
        message = Envelope(kind, f"{kind}-{name}", payload)
        JsonFrameCodec("client").encode(message)
        events = self.server.handle_message(message, self.identities[name])
        for event in events:
            JsonFrameCodec("server").encode(event.message)
        return events

    def create(self):
        return self.request("alice", "GROUP_CREATE", {
            "group": "study", "members": ["alice", "bob"],
        })

    def test_group_persists_and_is_visible_only_to_members(self):
        events = self.create()
        self.assertEqual({e.recipient.username for e in events}, {"alice", "bob"})
        self.assertTrue(all(e.message.type == "GROUP_LIST" for e in events))
        self.assertEqual(self.accounts.list_groups("eve"), [])
        other = AccountService(self.db)
        try:
            self.assertEqual(other.list_groups("bob"), [{
                "name": "study", "members": ["alice", "bob"], "owner": "alice",
            }])
        finally:
            other.close()
        self.server.close_session("bob")
        events = self.server.handle_message(
            Envelope("AUTH_REQUEST", "bob-again", {
                "username": "bob", "password": "synthetic-password",
            }), SessionIdentity("127.0.0.1", 10001, "bob-new"),
        )
        self.assertTrue(any(e.message.type == "GROUP_LIST" for e in events))

    def test_creator_is_owner_not_the_first_sorted_member(self):
        self.request("bob", "GROUP_CREATE", {"group": "bob-group", "members": ["alice", "bob"]})
        self.assertEqual(self.accounts.list_groups("alice")[0]["owner"], "bob")

    def test_only_owner_can_manage_even_when_other_member_is_global_admin(self):
        self.create()
        self.server._sessions["bob"] = SessionIdentity("127.0.0.1", 10000, "bob", "bob", "admin")
        for action, extra in (("delete", {}), ("kick", {"username": "alice"}), ("add", {"username": "eve"})):
            events = self.request("bob", "GROUP_MANAGE", {"group": "study", "action": action, **extra})
            self.assertEqual(events[0].message.payload["code"], "NOT_AUTHORIZED")
        self.assertEqual(self.accounts.list_groups("alice")[0]["members"], ["alice", "bob"])

    def test_owner_add_kick_delete_notify_all_affected_members(self):
        self.create()
        events = self.request("alice", "GROUP_MANAGE", {"group": "study", "action": "kick", "username": "bob"})
        self.assertEqual({e.recipient.username for e in events}, {"alice", "bob"})
        self.assertEqual(next(e.message.payload for e in events if e.recipient.username == "bob"), {"groups": []})
        self.assertEqual(self.accounts.list_groups("alice")[0]["members"], ["alice"])
        denied = self.request("bob", "CHAT_GROUP", {"group": "study", "text": "stale"})
        self.assertEqual(denied[0].message.payload["code"], "GROUP_NOT_FOUND")
        events = self.request("alice", "GROUP_MANAGE", {"group": "study", "action": "add", "username": "eve"})
        self.assertEqual({e.recipient.username for e in events}, {"alice", "eve"})
        self.assertEqual(self.accounts.list_groups("eve")[0]["owner"], "alice")
        events = self.request("alice", "GROUP_MANAGE", {"group": "study", "action": "delete"})
        self.assertEqual({e.recipient.username for e in events}, {"alice", "eve"})
        self.assertTrue(all(e.message.payload == {"groups": []} for e in events))
        self.assertEqual(self.accounts.list_groups("alice"), [])

    def test_leave_keeps_owner_and_owner_cannot_leave_or_kick_self(self):
        self.create()
        for action, extra in (("leave", {}), ("kick", {"username": "alice"})):
            events = self.request("alice", "GROUP_MANAGE", {"group": "study", "action": action, **extra})
            self.assertEqual(events[0].message.payload["code"], "GROUP_OWNER_CANNOT_LEAVE")
        events = self.request("bob", "GROUP_MANAGE", {"group": "study", "action": "leave"})
        self.assertEqual({e.recipient.username for e in events}, {"alice", "bob"})
        self.assertEqual(self.accounts.list_groups("alice"), [{
            "name": "study", "members": ["alice"], "owner": "alice",
        }])
        self.assertEqual(self.accounts.list_groups("bob"), [])

    def test_invalid_add_and_member_limit_do_not_change_membership(self):
        self.create()
        for username in ("missing", "bob"):
            with self.assertRaisesRegex(AccountError, "INVALID_GROUP_MEMBER"):
                self.accounts.manage_group("study", "alice", "add", username)
        with patch.dict("lancollab.server.accounts.FIELD_LIMITS", {"max_group_members": 2}):
            with self.assertRaisesRegex(AccountError, "GROUP_LIMIT_REACHED"):
                self.accounts.manage_group("study", "alice", "add", "eve")
        self.assertEqual(self.accounts.list_groups("alice")[0]["members"], ["alice", "bob"])

    def test_management_codec_rejects_forged_owner_and_wrong_fields(self):
        codec = JsonFrameCodec("client")
        for payload in (
            {"group": "study", "action": "delete", "owner": "alice"},
            {"group": "study", "action": "delete", "username": "alice"},
            {"group": "study", "action": "kick"},
            {"group": "study", "action": "transfer-owner", "username": "bob"},
        ):
            with self.assertRaises(OutboundMessageError):
                codec.encode(Envelope("GROUP_MANAGE", "invalid", payload))

    def test_removed_members_cannot_receive_or_send_new_group_file_offers(self):
        self.create()
        self.accounts.manage_group("study", "alice", "kick", "bob")
        payload = {
            "group": "study", "transfer_id": str(uuid4()), "name": "notes.txt",
            "size": 0, "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        }
        for sender, recipient in (("alice", "bob"), ("bob", "alice"), ("eve", "alice")):
            events = self.request(sender, "FILE_OFFER", {**payload, "to": recipient})
            self.assertEqual(events[0].message.payload["code"], "GROUP_NOT_FOUND")
        direct = dict(payload, to="alice")
        del direct["group"]
        events = self.request("bob", "FILE_OFFER", direct)
        self.assertEqual(events[0].message.type, "FILE_OFFER")  # Private files remain independent.

    def test_leave_updates_client_selection_without_falling_back_to_global(self):
        self.create()
        transport, peer = FakeTransport.pair()
        output = []
        shell = ConsoleSession(transport, output=output.append, downloads_dir=self.tmp.name)
        self.addCleanup(shell.close)
        shell.client.state, shell.client.username = ClientState.AUTHENTICATED, "bob"
        shell.client.handle_event(Envelope("GROUP_LIST", "restore", {"groups": self.accounts.list_groups("bob")}))
        shell.execute(parse_command("/group open study"))
        shell.execute(parse_command("/group leave study"))
        request = peer.receive()
        for event in self.server.handle_message(request, self.identities["bob"]):
            if event.recipient.username == "bob":
                shell.client.handle_event(event.message)
        shell._drain()
        self.assertIsNone(shell.current_group)
        self.assertEqual(shell.display_filter, "groups")
        self.assertEqual(shell.client.group_owners, {})
        with self.assertRaises(ValueError):
            shell.client.send_group("study", "must not send")
        with self.assertRaises(ValueError):
            shell.client.offer_group_file("unused.txt", "study")
        self.assertTrue(any("Access ended for #study" in text for text in output))

    def test_routing_excludes_sender_and_outsiders_and_requires_auth(self):
        self.create()
        events = self.request("alice", "CHAT_GROUP", {"group": "study", "text": "hello"})
        routed = [e for e in events if e.message.type == "CHAT_GROUP"]
        self.assertEqual([e.recipient.username for e in routed], ["bob"])
        self.assertEqual(routed[0].message.payload, {
            "group": "study", "from": "alice", "text": "hello",
        })
        self.assertEqual(events[-1].message.type, "CHAT_ACK")
        denied = self.request("eve", "CHAT_GROUP", {"group": "study", "text": "intrusion"})
        self.assertEqual(denied[0].message.payload["code"], "GROUP_NOT_FOUND")
        self.assertEqual(self.request("eve", "GROUP_LIST_REQUEST", {})[0].message.payload,
                         {"groups": []})
        anonymous = self.server.handle_message(
            Envelope("GROUP_LIST_REQUEST", "anonymous", {}),
            SessionIdentity("127.0.0.1", 10002, "anonymous"),
        )
        self.assertEqual(anonymous[0].message.payload["code"], "NOT_AUTHENTICATED")
        self.server.close_session("alice")
        stale = self.request("alice", "CHAT_GROUP", {"group": "study", "text": "stale"})
        self.assertEqual(stale[0].message.payload["code"], "NOT_AUTHENTICATED")

    def test_invalid_members_duplicate_names_and_capacity_are_atomic(self):
        self.accounts.create_account("pending", "synthetic-password")
        for members in (["alice", "pending"], ["alice", "missing"], ["bob", "eve"]):
            with self.assertRaises(AccountError):
                self.accounts.create_group("invalid", members, "alice")
            self.assertEqual(self.accounts.list_groups("alice"), [])
        self.create()
        with self.assertRaisesRegex(AccountError, "GROUP_EXISTS"):
            self.accounts.create_group("study", ["alice", "eve"], "alice")
        for number in range(127):
            self.accounts.create_group(f"g{number}", ["alice", "bob"], "alice")
        with self.assertRaisesRegex(AccountError, "GROUP_LIMIT_REACHED"):
            self.accounts.create_group("overflow", ["alice", "bob"], "alice")
        snapshot = self.request("alice", "GROUP_LIST_REQUEST", {})[0].message
        self.assertEqual(len(snapshot.payload["groups"]), 128)

    def test_codec_rejects_spoofing_bad_names_and_unbounded_members(self):
        codec = JsonFrameCodec("client")
        for payload in (
            {"group": "../study", "members": ["alice", "bob"]},
            {"group": "study", "members": ["alice", "alice"]},
            {"group": "study", "members": ["alice"]},
            {"group": "study", "members": [f"u{i}" for i in range(33)]},
            {"group": "study", "members": ["alice", 1]},
        ):
            with self.assertRaises(OutboundMessageError):
                codec.encode(Envelope("GROUP_CREATE", "bad", payload))
        with self.assertRaises(OutboundMessageError):
            codec.encode(Envelope("CHAT_GROUP", "spoof", {
                "group": "study", "text": "hello", "from": "bob",
            }))

    def test_client_commands_ack_history_and_rejected_send(self):
        transport, peer = FakeTransport.pair()
        history = HistoryStore(Path(self.tmp.name) / "history.db")
        self.addCleanup(history.close)
        client = CollabClient(transport, history=history, history_server="server",
                              downloads_dir=Path(self.tmp.name) / "downloads")
        self.addCleanup(client.close)
        client.state, client.username = ClientState.AUTHENTICATED, "alice"
        shell = ConsoleSession(transport, output=lambda _: None, downloads_dir=self.tmp.name)
        shell.client = client
        shell.execute(parse_command("/group create study bob"))
        request = peer.receive()
        for event in self.server.handle_message(request, self.identities["alice"]):
            if event.recipient.username == "alice":
                client.handle_event(event.message)
        shell.execute(parse_command("/group open study"))
        self.assertEqual((shell.current_group, shell.display_filter), ("study", "groups"))
        shell.execute(parse_command("/gmsg study hello"))
        request = peer.receive()
        for event in self.server.handle_message(request, self.identities["alice"]):
            if event.recipient.username == "alice":
                client.handle_event(event.message)
        client.handle_event(Envelope("CHAT_GROUP", "incoming", {
            "group": "study", "from": "bob", "text": "hi",
        }))
        notices = [notice.text for notice in client.drain_notices()]
        self.assertIn("[#study] you: hello", notices)
        self.assertIn("[#study] bob: hi", notices)
        self.assertIsNone(client.reply_target)
        self.assertEqual(history.people("server", "alice"), [])
        rows = history.recent("server", "alice")
        self.assertEqual([row[3] for row in rows], ["#study", "#study"])
        self.assertIn("[#study] bob: hi", shell.history_message(rows[-1]))
        with self.assertRaises(ValueError):
            client.send_group("secret", "not allowed")


class LegacyGroupTests(unittest.TestCase):
    def test_migration_preserves_members_and_requires_explicit_owner_recovery(self):
        with TemporaryDirectory() as directory:
            db = Path(directory) / "accounts.db"
            with sqlite3.connect(db) as connection:
                connection.execute("CREATE TABLE chat_groups(name TEXT PRIMARY KEY, members TEXT NOT NULL)")
                connection.execute("INSERT INTO chat_groups VALUES ('legacy', '[\"alice\", \"bob\"]')")
            connection.close()
            accounts = AccountService(db)
            try:
                accounts.create_account("alice", "synthetic-password", status="active")
                accounts.create_account("bob", "synthetic-password", status="active")
                self.assertEqual(accounts.list_groups("alice"), [{
                    "name": "legacy", "members": ["alice", "bob"],
                }])
                for actor in ("alice", "bob"):
                    with self.assertRaisesRegex(AccountError, "GROUP_OWNER_UNKNOWN"):
                        accounts.manage_group("legacy", actor, "delete")
                with self.assertRaisesRegex(AccountError, "INVALID_GROUP_MEMBER"):
                    accounts.recover_group_owner("legacy", "outsider")
                from lancollab.server.__main__ import main
                with (
                    patch("sys.argv", ["server", "--db", str(db), "--recover-group-owner", "legacy", "bob"]),
                    patch("lancollab.server.__main__.logging.basicConfig"),
                    patch("builtins.print"),
                ):
                    self.assertEqual(main(), 0)
                self.assertEqual(accounts.list_groups("alice")[0]["owner"], "bob")
                with self.assertRaisesRegex(AccountError, "GROUP_OWNER_ALREADY_SET"):
                    accounts.recover_group_owner("legacy", "alice")
                with self.assertRaisesRegex(AccountError, "NOT_AUTHORIZED"):
                    accounts.manage_group("legacy", "alice", "delete")
                accounts.manage_group("legacy", "bob", "delete")
                self.assertEqual(accounts.list_groups("bob"), [])
            finally:
                accounts.close()


class GroupFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        transport, self.peer = FakeTransport.pair()
        self.output = []
        self.shell = ConsoleSession(transport, output=self.output.append, downloads_dir=self.tmp.name)
        self.addCleanup(self.shell.close)
        self.client = self.shell.client
        self.client.state, self.client.username = ClientState.AUTHENTICATED, "alice"
        self.client.groups = {"study": ["alice", "bob", "carol", "offline"]}
        self.client.online_users = {name: ("127.0.0.1", 1) for name in ("alice", "bob", "carol", "eve")}
        self.source = Path(self.tmp.name) / "study notes.txt"
        self.source.write_bytes(b"notes for the group")

    def test_fanout_uses_independent_offers_only_for_other_online_members(self):
        offered = self.client.offer_group_file(self.source, "study")
        self.assertEqual(set(offered), {"bob", "carol"})
        self.assertEqual(len(set(offered.values())), 2)
        messages = [self.peer.receive(), self.peer.receive()]
        self.assertTrue(all(m.type == "FILE_OFFER" for m in messages))
        self.assertTrue(all(m.payload["group"] == "study" for m in messages))
        self.assertEqual({m.payload["to"] for m in messages}, {"bob", "carol"})
        self.assertTrue(all(m.payload["name"] == self.source.name for m in messages))
        with self.assertRaises(TimeoutError):
            self.peer.receive()  # No file bytes before either recipient consents.
        self.assertTrue(all(row["status"] == "waiting" for row in self.client.files.snapshot()))
        self.assertTrue(any("skipped offline members: offline" in n.text
                            for n in self.client.drain_notices()))

    def test_one_local_failure_does_not_prevent_other_offers(self):
        offer = self.client.offer_file

        def fail_bob(path, recipient, **kwargs):
            if recipient == "bob":
                raise TransferError("recipient unavailable")
            return offer(path, recipient, **kwargs)

        with patch.object(self.client, "offer_file", side_effect=fail_bob):
            offered = self.client.offer_group_file(self.source, "study")
        self.assertEqual(set(offered), {"carol"})
        self.assertEqual(self.peer.receive().payload["to"], "carol")
        self.assertTrue(any("bob: not offered" in n.text for n in self.client.drain_notices()))

    def test_missing_group_or_no_online_peers_never_offers(self):
        with patch.object(self.client, "offer_file") as offer:
            with self.assertRaises(ValueError):
                self.client.offer_group_file(self.source, "secret")
            self.client.online_users = {"alice": ("127.0.0.1", 1), "eve": ("127.0.0.1", 2)}
            with self.assertRaisesRegex(ValueError, "No other online members"):
                self.client.offer_group_file(self.source, "study")
            offer.assert_not_called()

    def test_invalid_file_is_reported_without_creating_transfers(self):
        self.assertEqual(self.client.offer_group_file(self.source / "missing", "study"), {})
        self.assertEqual(self.client.files.snapshot(), [])
        self.assertEqual(self.client.state, ClientState.AUTHENTICATED)
        self.assertEqual(sum("not offered" in n.text for n in self.client.drain_notices()), 2)

    def test_cli_preserves_quoted_paths_and_supports_group_alias(self):
        for command in ("/sendgroup study", "/send #study"):
            with self.subTest(command=command), patch.object(self.client, "offer_group_file") as offer:
                parsed = parse_command(command + r' "C:\My files\notes.txt"')
                self.shell.execute(parsed)
                offer.assert_called_once_with(r"C:\My files\notes.txt", "study")
        with self.assertRaises(ValueError):
            parse_command("/sendgroup study")


class GroupTerminalTests(unittest.TestCase):
    def session(self):
        session = FakeSession()
        session.client.groups = {"study": ["alice", "bob"], "other": ["alice", "eve"]}
        session.current_group = "study"
        session.display_filter = "groups"
        return session

    def test_groups_follow_global_in_tabs_f3_and_alt_shortcuts(self):
        session = self.session()
        session.display_filter = "all"

        def ordered(window):
            tabs = window.lines[3]
            labels = ("1:All", "2:Global", "3:Groups", "4:Direct", "5:Files")
            return [tabs.index(label) for label in labels] == sorted(tabs.index(label) for label in labels)

        run(session, [
            expect(ordered, lambda w: w.module.KEY_F3),
            expect(lambda w: session.display_filter == "global", lambda w: w.module.KEY_F3),
            expect(lambda w: session.display_filter == "groups", "\x1b4"),
            expect(lambda w: session.display_filter == "dm", "\x03"),
        ])

    def test_group_text_never_falls_back_to_global_and_filters_other_groups(self):
        session = self.session()

        def incoming(window):
            session.output("[#study] bob: visible")
            session.output("[#other] eve: hidden")
            return ""

        run(session, [
            incoming,
            expect(lambda w: "visible" in w.all_text() and "hidden" not in w.all_text(),
                   "hello\n"),
            "\x03",
        ])
        self.assertEqual(session.executed[-1].name, "/gmsg")
        self.assertEqual(session.executed[-1].args, ("study", "hello"))

    def test_no_selection_cannot_send_global_and_group_tab_fits_narrow_screen(self):
        session = self.session()
        session.current_group = None
        run(session, [
            expect(lambda w: "3:Groups" in w.all_text(), "hello\n"),
            "\x03",
        ], rows=16, columns=40)
        self.assertEqual(session.executed, [])

    def test_sidebar_group_opens_via_command(self):
        session = self.session()
        run(session, [click("#study", occurrence=2), "\x03"])
        self.assertEqual(session.executed[-1].args, ("open", "study"))

    def test_owner_menu_kicks_and_confirms_delete(self):
        session = self.session()
        session.client.group_owners = {"study": "alice"}
        run(session, [click("[Manage]"), click("Kick bob"), "\x03"])
        self.assertEqual(session.executed[-1].args, ("kick", "study", "bob"))
        session = self.session()
        session.client.group_owners = {"study": "alice"}
        run(session, [
            click("[Manage]"), click("Delete group..."),
            expect(lambda w: not session.executed, click("Confirm delete #study")),
            "\x03",
        ])
        self.assertEqual(session.executed[-1].args, ("delete", "study"))

    def test_members_only_see_leave_and_delete_confirmation_can_be_cancelled(self):
        session = self.session()
        session.client.group_owners = {"study": "bob"}
        run(session, [
            expect(lambda w: "[Manage]" not in w.all_text(), click("[Leave]")),
            click("Confirm leave #study"), "\x03",
        ])
        self.assertEqual(session.executed[-1].args, ("leave", "study"))
        session = self.session()
        session.client.group_owners = {"study": "alice"}
        run(session, [click("[Manage]"), click("Delete group..."), click("Cancel"), "\x03"])
        self.assertEqual(session.executed, [])

    def test_add_member_template_preserves_existing_draft(self):
        session = self.session()
        session.client.group_owners = {"study": "alice"}
        run(session, [
            "draft", click("[Manage]"), click("Add member..."),
            expect(lambda w: "draft" in w.input_text(), "\x03"),
        ])
        session = self.session()
        session.client.group_owners = {"study": "alice"}
        run(session, [
            click("[Manage]"), click("Add member..."),
            expect(lambda w: "/group add study " in w.input_text(), "\x03"),
        ])

    def test_group_file_button_uses_picker_and_cancel_sends_nothing(self):
        for path in (r"C:\My files\notes.txt", ""):
            with self.subTest(path=path):
                session = self.session()
                with patch("lancollab.client.tui.choose_file", return_value=path):
                    run(session, [click("[Send file]"), "\x03"])
                self.assertEqual(
                    [(command.name, command.args) for command in session.executed],
                    [("/sendgroup", ("study", path))] if path else [],
                )

    def test_group_file_picker_error_and_file_notices_stay_visible(self):
        session = self.session()
        with patch("lancollab.client.tui.choose_file", side_effect=RuntimeError("Picker unavailable")):
            run(session, [
                click("[Send file]"),
                expect(lambda w: "Picker unavailable" in w.all_text(), "\x03"),
            ])
        self.assertEqual(session.executed, [])
        session = self.session()

        def offline_notice(window):
            session.output("[file] #study: skipped offline members: bob")
            return ""

        run(session, [
            offline_notice,
            expect(lambda w: "skipped offline members: bob" in w.all_text(), "\x03"),
        ])

    def test_switching_groups_keeps_drafts_separate(self):
        session = self.session()
        execute = session.execute

        def open_group(command):
            if command.name == "/group" and command.args[0] == "open":
                session.current_group = command.args[1]
            return execute(command)

        session.execute = open_group
        run(session, [
            "study draft",
            click("#other"),
            expect(lambda w: "study draft" not in w.input_text(), "other draft"),
            click("#study"),
            expect(lambda w: "study draft" in w.input_text() and "other draft" not in w.input_text(),
                   "\x03"),
        ])


if __name__ == "__main__":
    unittest.main()
