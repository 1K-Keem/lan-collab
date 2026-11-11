"""Run: python -m tests.check_direct_history.

Direct inbox/transcript: private-only, server/account isolated, no 50-row cap.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from lancollab.client.cli import ConsoleSession
from lancollab.client.history import HistoryStore
from tests.fake_transport import FakeTransport

def main():
    with TemporaryDirectory() as directory:
        store = HistoryStore(Path(directory) / "history.sqlite3")
        try:
            for server, account, direction, kind, peer, text in [
                ("server-a", "alice", "received", "CHAT_PRIVATE", "bob", "old from bob"),
                ("server-a", "alice", "sent", "CHAT_PRIVATE", "bob", "new to bob"),
                ("server-a", "alice", "received", "CHAT_GLOBAL", "bob", "global excluded"),
                ("server-a", "alice", "received", "CHAT_PRIVATE", "carol", "from carol"),
                ("server-b", "alice", "received", "CHAT_PRIVATE", "bob", "other server"),
                ("server-a", "dave", "received", "CHAT_PRIVATE", "bob", "other account"),
                ("server-a", "alice", "received", "CHAT_PRIVATE", "alice", "self excluded"),
            ]:
                store.append(server, account, direction, kind, peer, text)

            assert [
                (peer, text)
                for peer, _timestamp, text in store.direct_conversations("server-a", "alice")
            ] == [("carol", "from carol"), ("bob", "new to bob")]

            for number in range(60):
                store.append(
                    "server-a", "alice", "received", "CHAT_PRIVATE", "bob", f"bulk {number}"
                )
            rows = store.direct_transcript("server-a", "alice", "bob")
            assert len(rows) == 62, len(rows)  # no 50-message cap
            assert [row[4] for row in rows[:2]] == ["old from bob", "new to bob"]
            assert [row[4] for row in rows[-2:]] == ["bulk 58", "bulk 59"]
            assert {row[2] for row in rows} == {"CHAT_PRIVATE"}
            assert all(row[3] == "bob" and row[0] for row in rows)
            assert len(store.direct_transcript("server-b", "alice", "bob")) == 1
            assert len(store.direct_transcript("server-a", "dave", "bob")) == 1
        finally:
            store.close()

    left, _right = FakeTransport.pair()
    shell = ConsoleSession(left)
    try:
        shell.client.username = "alice"
        shell.client._remember_chat("received", "CHAT_PRIVATE", "bob", "hi")
        shell.client._remember_chat("sent", "CHAT_PRIVATE", "bob", "yo")
        shell.client._remember_chat("sent", "CHAT_GLOBAL", "*", "global")
        shell.client._remember_chat("received", "CHAT_PRIVATE", "carol", "hey")
        shell.client._remember_chat("received", "CHAT_PRIVATE", "alice", "self")
        summary = shell.direct_conversations()
        assert [(peer, text) for peer, _timestamp, text in summary] == [
            ("carol", "hey"),
            ("bob", "yo"),
        ]
        assert [
            (row[1], row[4]) for row in shell.direct_transcript("bob")
        ] == [("received", "hi"), ("sent", "yo")]
        assert shell.direct_transcript("alice") == []
    finally:
        shell.close()

    print("Direct history: private-only, isolated, complete transcript")

if __name__ == "__main__":
    main()
