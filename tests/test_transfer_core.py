"""Transfer core: server authorization/credit rules plus sender/receiver end-to-end flows."""

from typing import Any
import base64
import hashlib
import os
import random
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

from lancollab.common.contracts import Envelope, SessionIdentity
from lancollab.common.filenames import sanitize_filename
from lancollab.common.transfer_core import (
    AckLedger,
    DiskSink,
    IncomingTransfer,
    OutgoingTransfer,
    TransferProtocolError,
    TransferRegistry,
    TransferState,
)

CHUNK = 16384


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def ident(name: str, n: int = 1) -> SessionIdentity:
    return SessionIdentity("192.0.2.1", 50000 + n, f"s-{name}-{n}", name)


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def offer_msg(
    tid: str, to: str, size: int, sha: str | None = None, name: str = "f.bin"
) -> Envelope:
    sha = sha or hashlib.sha256(b"x").hexdigest()
    payload = {"transfer_id": tid, "to": to, "name": name, "size": size, "sha256": sha}
    return Envelope("FILE_OFFER", "req-offer", payload)


def msg(kind: str, **payload: object) -> Envelope:
    return Envelope(kind, "req-" + kind.lower(), dict(payload))


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = Clock()
        self.reg = TransferRegistry(clock=self.clock)
        self.alice, self.bob, self.carol = ident("alice"), ident("bob"), ident("carol")
        for who in (self.alice, self.bob, self.carol):
            self.reg.open_session(who)

    def new_offer(self, size: int = 100, sender=None, receiver=None) -> str:
        tid = str(uuid.uuid4())
        sender, receiver = sender or self.alice, receiver or self.bob
        assert receiver.username is not None
        out = self.reg.offer(sender, receiver, offer_msg(tid, receiver.username, size))
        self.assertIsNone(out.error, out.error)
        return tid

    def accepted(self, size: int = 100) -> str:
        tid = self.new_offer(size)
        self.assertIsNone(self.reg.handle(self.bob, msg("FILE_ACCEPT", transfer_id=tid)).error)
        return tid

    def chunk(self, tid: str, seq: int, offset: int, data: bytes) -> Envelope:
        return msg("FILE_CHUNK", transfer_id=tid, seq=seq, offset=offset, data_b64=b64(data))

    def assertError(self, out, code: str) -> None:
        self.assertIsNotNone(out.error, "expected an error")
        self.assertEqual(out.error.code, code)

    def cancel_targets(self, out) -> dict[str, str]:
        return {
            f.recipient.username: f.message.payload["reason"]
            for f in out.forwards
            if f.message.type == "FILE_CANCEL"
        }


class OfferTests(RegistryCase):
    def test_offer_forwards_event_with_authenticated_sender_name(self) -> None:
        tid = str(uuid.uuid4())
        out = self.reg.offer(self.alice, self.bob, offer_msg(tid, "bob", 5))
        (fwd,) = out.forwards
        self.assertEqual(
            (fwd.recipient, fwd.lane, fwd.message.type), (self.bob, "control", "FILE_OFFER")
        )
        self.assertEqual(fwd.message.payload["from"], "alice")
        self.assertNotEqual(fwd.message.request_id, "req-offer")  # fresh server-generated id

    def test_rejections(self) -> None:
        tid = self.new_offer()
        cases = [
            (
                self.reg.offer(self.alice, self.bob, offer_msg(tid, "bob", 5)),
                "TRANSFER_ID_CONFLICT",
            ),
            (
                self.reg.offer(self.carol, self.alice, offer_msg(tid, "alice", 5)),
                "TRANSFER_ID_CONFLICT",
            ),
            (
                self.reg.offer(self.carol, self.bob, offer_msg(str(uuid.uuid4()), "bob", 5)),
                "RECIPIENT_BUSY",
            ),
            (
                self.reg.offer(self.alice, self.alice, offer_msg(str(uuid.uuid4()), "alice", 5)),
                "INVALID_MESSAGE",
            ),
            (
                self.reg.offer(
                    self.alice, ident("alice", 2), offer_msg(str(uuid.uuid4()), "alice", 5)
                ),
                "USER_OFFLINE",
            ),
            (
                self.reg.offer(
                    self.alice,
                    self.carol,
                    offer_msg(str(uuid.uuid4()), "carol", 50 * 1024 * 1024 + 1),
                ),
                "FILE_TOO_LARGE",
            ),
            (
                self.reg.offer(self.alice, self.carol, offer_msg(str(uuid.uuid4()), "bob", 5)),
                "INVALID_MESSAGE",
            ),
            (
                self.reg.offer(
                    ident("mallory"), self.carol, offer_msg(str(uuid.uuid4()), "carol", 5)
                ),
                "NOT_AUTHENTICATED",
            ),
        ]
        for out, code in cases:
            with self.subTest(code=code):
                self.assertError(out, code)

    def test_same_uuid_cannot_be_used_in_both_directions_at_once(self) -> None:
        tid = self.new_offer(sender=self.alice, receiver=self.bob)
        out = self.reg.offer(self.bob, self.carol, offer_msg(tid, "carol", 5))
        self.assertError(out, "TRANSFER_ID_CONFLICT")

    def test_terminal_ids_cannot_be_reused_by_either_participant(self) -> None:
        tid = self.new_offer()
        self.reg.handle(self.alice, msg("FILE_CANCEL", transfer_id=tid, reason="CHANGED_MIND"))
        for sender, receiver in (
            (self.alice, self.bob),
            (self.carol, self.bob),
            (self.alice, self.carol),
        ):
            assert receiver.username is not None
            out = self.reg.offer(sender, receiver, offer_msg(tid, receiver.username, 5))
            self.assertError(out, "TRANSFER_ID_CONFLICT")

    def test_per_session_offer_cap_and_busy_release(self) -> None:
        for _ in range(1024):
            tid = self.new_offer()
            self.reg.handle(self.bob, msg("FILE_REJECT", transfer_id=tid))
        out = self.reg.offer(self.alice, self.bob, offer_msg(str(uuid.uuid4()), "bob", 5))
        self.assertError(out, "TRANSFER_LIMIT_REACHED")
        # Reconnecting creates a fresh session with an empty history.
        alice2 = ident("alice", 2)
        self.reg.open_session(alice2)
        self.assertIsNone(
            self.reg.offer(alice2, self.carol, offer_msg(str(uuid.uuid4()), "carol", 5)).error
        )


class AuthorizationTests(RegistryCase):
    def test_third_party_cannot_touch_a_transfer_it_knows_the_uuid_of(self) -> None:
        tid = self.accepted()
        for kind, extra in (
            ("FILE_ACCEPT", {}),
            ("FILE_REJECT", {}),
            ("FILE_CANCEL", {"reason": "X"}),
            ("FILE_ACK", {"next_offset": 0}),
            ("FILE_END", {"total_chunks": 0}),
            ("FILE_RESULT", {"ok": True}),
            ("FILE_CHUNK", {"seq": 0, "offset": 0, "data_b64": b64(b"x")}),
        ):
            with self.subTest(kind=kind):
                out = self.reg.handle(self.carol, msg(kind, transfer_id=tid, **extra))
                self.assertError(out, "TRANSFER_NOT_FOUND")
                self.assertEqual(out.forwards, ())
        self.assertIs(self.reg.state_of(tid), TransferState.ACCEPTED)

    def test_roles_are_enforced_without_cancelling(self) -> None:
        tid = self.accepted()
        wrong_for_sender = [
            msg("FILE_ACCEPT", transfer_id=tid),
            msg("FILE_REJECT", transfer_id=tid),
            msg("FILE_ACK", transfer_id=tid, next_offset=0),
            msg("FILE_RESULT", transfer_id=tid, ok=True),
        ]
        for m in wrong_for_sender:
            self.assertError(self.reg.handle(self.alice, m), "INVALID_STATE")
        for m in (self.chunk(tid, 0, 0, b"x"), msg("FILE_END", transfer_id=tid, total_chunks=0)):
            self.assertError(self.reg.handle(self.bob, m), "INVALID_STATE")
        self.assertIs(self.reg.state_of(tid), TransferState.ACCEPTED)

    def test_accept_only_once_and_only_while_offered(self) -> None:
        tid = self.accepted()
        self.assertError(
            self.reg.handle(self.bob, msg("FILE_ACCEPT", transfer_id=tid)), "INVALID_STATE"
        )
        self.assertError(
            self.reg.handle(self.bob, msg("FILE_REJECT", transfer_id=tid)), "INVALID_STATE"
        )

    def test_unregistered_session_is_not_authenticated(self) -> None:
        tid = self.new_offer()
        out = self.reg.handle(ident("bob", 99), msg("FILE_ACCEPT", transfer_id=tid))
        self.assertError(out, "NOT_AUTHENTICATED")

    def test_reject_notifies_sender_and_frees_receiver(self) -> None:
        tid = self.new_offer()
        out = self.reg.handle(self.bob, msg("FILE_REJECT", transfer_id=tid, reason="no thanks"))
        (fwd,) = out.forwards
        self.assertEqual((fwd.recipient, fwd.message.type), (self.alice, "FILE_REJECT"))
        self.assertEqual(out.purge, (tid,))
        self.assertIsNone(self.reg.state_of(tid))
        self.new_offer(receiver=self.bob)  # bob is free again
        self.assertError(
            self.reg.handle(self.alice, msg("FILE_CANCEL", transfer_id=tid, reason="x")),
            "TRANSFER_NOT_FOUND",
        )

    def test_cancel_notifies_only_the_other_participant(self) -> None:
        tid = self.accepted()
        out = self.reg.handle(self.bob, msg("FILE_CANCEL", transfer_id=tid, reason="USER_ABORT"))
        self.assertEqual(self.cancel_targets(out), {"alice": "USER_ABORT"})
        self.assertEqual(out.purge, (tid,))
        self.assertEqual(
            self.reg.handle(self.bob, msg("FILE_CANCEL", transfer_id=tid, reason="again")).forwards,
            (),
        )


class ChunkAndWindowTests(RegistryCase):
    def test_in_order_chunks_are_forwarded_on_the_file_lane(self) -> None:
        tid = self.accepted(size=CHUNK + 5)
        out = self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"a" * CHUNK))
        (fwd,) = out.forwards
        self.assertEqual((fwd.recipient, fwd.lane), (self.bob, "file"))
        self.assertEqual(fwd.message.payload["offset"], 0)
        out = self.reg.handle(self.alice, self.chunk(tid, 1, CHUNK, b"b" * 5))
        self.assertEqual(out.forwards[0].message.payload["seq"], 1)

    def test_bad_chunks_never_reach_receiver_and_abort_transfer(self) -> None:
        bad_cases = [
            ("seq", lambda t: self.chunk(t, 1, 0, b"x"), "INVALID_CHUNK_OFFSET"),
            ("offset", lambda t: self.chunk(t, 0, 5, b"x"), "INVALID_CHUNK_OFFSET"),
            ("oversize", lambda t: self.chunk(t, 0, 0, b"x" * 101), "INVALID_CHUNK_OFFSET"),
        ]
        for label, build, code in bad_cases:
            with self.subTest(label):
                tid = self.accepted(size=100)
                out = self.reg.handle(self.alice, build(tid))
                self.assertError(out, code)
                self.assertFalse([f for f in out.forwards if f.message.type == "FILE_CHUNK"])
                self.assertEqual(self.cancel_targets(out), {"alice": code, "bob": code})
                self.assertIsNone(self.reg.state_of(tid))
                self.assertEqual(out.purge, (tid,))
                self.reg.handle(self.alice, msg("FILE_CANCEL", transfer_id=tid, reason="x"))

    def test_chunk_before_accept_is_a_violation(self) -> None:
        tid = self.new_offer()
        out = self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"x"))
        self.assertError(out, "INVALID_STATE")
        self.assertIsNone(self.reg.state_of(tid))

    def test_ninth_unacknowledged_chunk_violates_window(self) -> None:
        tid = self.accepted(size=100)
        for n in range(8):
            self.assertIsNone(self.reg.handle(self.alice, self.chunk(tid, n, n, b"x")).error)
        out = self.reg.handle(self.alice, self.chunk(tid, 8, 8, b"x"))
        self.assertError(out, "INVALID_STATE")
        self.assertIsNone(self.reg.state_of(tid))

    def test_receiver_ack_reopens_the_window(self) -> None:
        tid = self.accepted(size=100)
        for n in range(8):
            self.reg.handle(self.alice, self.chunk(tid, n, n, b"x"))
        ack = self.reg.handle(self.bob, msg("FILE_ACK", transfer_id=tid, next_offset=4))
        (fwd,) = ack.forwards
        self.assertEqual(
            (fwd.recipient, fwd.lane, fwd.message.payload["next_offset"]),
            (self.alice, "control", 4),
        )
        for n in range(8, 12):
            self.assertIsNone(self.reg.handle(self.alice, self.chunk(tid, n, n, b"x")).error)
        self.assertError(
            self.reg.handle(self.alice, self.chunk(tid, 12, 12, b"x")), "INVALID_STATE"
        )

    def test_queue_full_cancels_only_that_transfer(self) -> None:
        tid = self.accepted()
        other = self.new_offer(sender=self.alice, receiver=self.carol)
        out = self.reg.fail_queue_full(tid)
        self.assertEqual(self.cancel_targets(out), {"alice": "QUEUE_FULL", "bob": "QUEUE_FULL"})
        self.assertEqual(out.purge, (tid,))
        self.assertIs(self.reg.state_of(other), TransferState.OFFERED)
        self.assertEqual(self.reg.fail_queue_full(tid).forwards, ())


class AckTests(RegistryCase):
    def streaming(self) -> str:
        tid = self.accepted(size=40)
        for n in range(4):
            self.reg.handle(self.alice, self.chunk(tid, n, n * 10, b"x" * 10))
        return tid

    def ack(self, tid: str, offset: int):
        return self.reg.handle(self.bob, msg("FILE_ACK", transfer_id=tid, next_offset=offset))

    def test_non_boundary_decreasing_and_oversized_acks_abort(self) -> None:
        for bad in (15, 41, 100):
            with self.subTest(offset=bad):
                tid = self.streaming()
                self.assertError(self.ack(tid, bad), "INVALID_STATE")
                self.assertIsNone(self.reg.state_of(tid))
                self.reg.handle(self.alice, msg("FILE_CANCEL", transfer_id=tid, reason="x"))
        tid = self.streaming()
        self.assertIsNone(self.ack(tid, 20).error)
        self.assertError(self.ack(tid, 10), "INVALID_STATE")  # decreasing

    def test_duplicate_ack_is_ignored_and_does_not_extend_stall_timer(self) -> None:
        tid = self.streaming()
        self.ack(tid, 20)
        self.clock.now += 10
        dup = self.ack(tid, 20)
        self.assertEqual((dup.forwards, dup.error), ((), None))
        self.clock.now += 6  # 16s after the last real progress (the ACK)
        self.assertEqual(
            self.cancel_targets(self.reg.expire()), {"alice": "TIMEOUT", "bob": "TIMEOUT"}
        )

    def test_ack_only_valid_in_data_states(self) -> None:
        tid = self.new_offer(size=10)  # still OFFERED
        self.assertError(self.ack(tid, 0), "INVALID_STATE")
        tid = self.accepted(size=10)  # ACCEPTED: only the empty-file ACK 0 is allowed
        self.assertError(self.ack(tid, 0), "INVALID_STATE")

    def test_empty_file_flow_requires_ack_zero_before_success(self) -> None:
        tid = self.accepted(size=0)
        out = self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=0))
        self.assertEqual(out.forwards[0].lane, "file")
        self.assertIs(self.reg.state_of(tid), TransferState.VERIFYING)
        early = self.reg.handle(self.bob, msg("FILE_RESULT", transfer_id=tid, ok=True))
        self.assertError(early, "INVALID_STATE")  # ACK 0 must come first
        self.assertIsNone(self.reg.state_of(tid))

        tid = self.accepted(size=0)
        self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=0))
        self.assertIsNone(self.ack(tid, 0).error)
        done = self.reg.handle(self.bob, msg("FILE_RESULT", transfer_id=tid, ok=True))
        self.assertEqual(done.forwards[0].message.type, "FILE_RESULT")
        self.assertIsNone(self.reg.state_of(tid))


class EndAndResultTests(RegistryCase):
    def sent(self, size: int, chunks: int) -> str:
        tid = self.accepted(size=size)
        per = size // chunks
        for n in range(chunks):
            self.reg.handle(self.alice, self.chunk(tid, n, n * per, b"x" * per))
        return tid

    def test_end_guards(self) -> None:
        tid = self.accepted(size=30)
        self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"x" * 10))
        self.assertError(
            self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=1)),
            "INVALID_STATE",
        )
        tid = self.sent(30, 3)
        self.assertError(
            self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=2)),
            "INVALID_STATE",
        )
        tid = self.accepted(size=30)  # END from ACCEPTED for a non-empty file
        self.assertError(
            self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=0)),
            "INVALID_STATE",
        )

    def test_final_ack_is_valid_in_verifying_and_result_follows(self) -> None:
        tid = self.sent(40, 4)
        self.reg.handle(self.bob, msg("FILE_ACK", transfer_id=tid, next_offset=40))
        out = self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=4))
        self.assertEqual(out.forwards[0].message.type, "FILE_END")
        res = self.reg.handle(self.bob, msg("FILE_RESULT", transfer_id=tid, ok=True))
        self.assertEqual(res.forwards[0].recipient, self.alice)
        self.assertIsNone(self.reg.state_of(tid))

    def test_end_before_ack_drain_then_late_final_ack(self) -> None:
        tid = self.sent(40, 4)
        self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=4))  # no ACK yet
        self.assertIs(self.reg.state_of(tid), TransferState.VERIFYING)
        self.assertIsNone(
            self.reg.handle(self.bob, msg("FILE_ACK", transfer_id=tid, next_offset=40)).error
        )
        self.assertIsNone(
            self.reg.handle(self.bob, msg("FILE_RESULT", transfer_id=tid, ok=True)).error
        )

    def test_premature_or_unacked_success_is_rejected(self) -> None:
        tid = self.sent(40, 4)
        self.assertError(
            self.reg.handle(self.bob, msg("FILE_RESULT", transfer_id=tid, ok=True)), "INVALID_STATE"
        )
        tid = self.sent(40, 4)
        self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=4))
        out = self.reg.handle(self.bob, msg("FILE_RESULT", transfer_id=tid, ok=True))
        self.assertError(out, "INVALID_STATE")  # final ACK never arrived
        self.assertEqual(
            self.cancel_targets(out), {"alice": "INVALID_STATE", "bob": "INVALID_STATE"}
        )

    def test_failed_result_is_forwarded_as_failure_not_success(self) -> None:
        tid = self.sent(40, 4)
        self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=4))
        out = self.reg.handle(
            self.bob, msg("FILE_RESULT", transfer_id=tid, ok=False, reason="HASH_MISMATCH")
        )
        (fwd,) = out.forwards
        self.assertFalse(fwd.message.payload["ok"])
        self.assertIsNone(self.reg.state_of(tid))


class TimerTests(RegistryCase):
    def kinds(self) -> dict[str, str]:
        return self.cancel_targets(self.reg.expire())

    def test_no_timer_runs_until_the_write_is_confirmed(self) -> None:
        self.new_offer()
        self.clock.now += 10_000
        self.assertEqual(self.reg.expire().forwards, ())

    def test_offer_timeout_starts_after_offer_written(self) -> None:
        tid = self.new_offer()
        self.reg.mark_written(tid, "FILE_OFFER")
        self.clock.now += 59
        self.assertEqual(self.kinds(), {})
        self.clock.now += 2
        self.assertEqual(self.kinds(), {"alice": "TIMEOUT", "bob": "TIMEOUT"})
        self.assertIsNone(self.reg.state_of(tid))

    def test_first_data_timeout_after_accept_written(self) -> None:
        tid = self.accepted()
        self.reg.mark_written(tid, "FILE_ACCEPT")
        self.clock.now += 9
        self.assertEqual(self.kinds(), {})
        self.clock.now += 2  # 11s after ACCEPT was written
        self.assertEqual(self.kinds(), {"alice": "TIMEOUT", "bob": "TIMEOUT"})
        self.assertIsNone(self.reg.state_of(tid))

    def test_stall_timer_resets_on_progress_only(self) -> None:
        tid = self.accepted(size=100)
        self.reg.mark_written(tid, "FILE_ACCEPT")
        self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"x" * 10))
        self.clock.now += 14
        self.reg.handle(self.alice, self.chunk(tid, 1, 10, b"x" * 10))  # progress resets
        self.clock.now += 14
        self.assertEqual(self.kinds(), {})
        self.clock.now += 2
        self.assertEqual(self.kinds(), {"alice": "TIMEOUT", "bob": "TIMEOUT"})

    def test_stall_stays_active_until_end_is_written_then_verify_timer_takes_over(self) -> None:
        tid = self.accepted(size=20)
        self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"x" * 10))
        self.reg.handle(self.alice, self.chunk(tid, 1, 10, b"x" * 10))
        self.reg.handle(self.alice, msg("FILE_END", transfer_id=tid, total_chunks=2))
        self.clock.now += 14
        self.reg.mark_written(tid, "FILE_END")  # verify deadline = +30 from now
        self.clock.now += 29
        self.reg.handle(self.bob, msg("FILE_ACK", transfer_id=tid, next_offset=20))  # no extension
        self.assertEqual(self.kinds(), {})
        self.clock.now += 2
        self.assertEqual(self.kinds(), {"alice": "TIMEOUT", "bob": "TIMEOUT"})

    def test_missing_end_trips_the_stall_watchdog(self) -> None:
        tid = self.accepted(size=10)
        self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"x" * 10))
        self.clock.now += 16
        self.assertEqual(self.kinds(), {"alice": "TIMEOUT", "bob": "TIMEOUT"})


class SessionLifecycleTests(RegistryCase):
    def test_takeover_cancels_only_for_the_survivor_and_new_session_is_clean(self) -> None:
        tid = self.accepted(size=100)
        self.reg.handle(self.alice, self.chunk(tid, 0, 0, b"x"))
        out = self.reg.close_session(self.bob.session_id, "SESSION_REPLACED")
        self.assertEqual(self.cancel_targets(out), {"alice": "SESSION_REPLACED"})
        self.assertEqual(out.purge, (tid,))
        self.assertIsNone(self.reg.state_of(tid))
        # stale pipelined requests from the replaced session cannot act
        self.assertError(
            self.reg.handle(self.bob, msg("FILE_ACK", transfer_id=tid, next_offset=1)),
            "NOT_AUTHENTICATED",
        )
        # replacement connection of the same user gets no old state and can receive again
        bob2 = ident("bob", 2)
        self.reg.open_session(bob2)
        new_tid = str(uuid.uuid4())
        self.assertIsNone(self.reg.offer(self.alice, bob2, offer_msg(new_tid, "bob", 5)).error)
        self.assertError(
            self.reg.handle(bob2, msg("FILE_ACK", transfer_id=tid, next_offset=1)),
            "TRANSFER_NOT_FOUND",
        )

    def test_close_is_idempotent_and_cleans_both_roles(self) -> None:
        self.accepted()
        other = self.new_offer(sender=self.carol, receiver=self.alice)
        out = self.reg.close_session(self.alice.session_id)
        self.assertEqual(self.cancel_targets(out), {"bob": "DISCONNECTED", "carol": "DISCONNECTED"})
        self.assertIsNone(self.reg.state_of(other))
        self.assertEqual(self.reg.close_session(self.alice.session_id).forwards, ())

    def test_no_notice_is_built_for_a_peer_that_already_left(self) -> None:
        tid = self.accepted()
        self.reg.close_session(self.bob.session_id)
        self.assertIsNone(self.reg.state_of(tid))
        out = self.reg.fail_queue_full(tid)
        self.assertEqual(out.forwards, ())

    def test_open_session_rules(self) -> None:
        with self.assertRaises(ValueError):
            self.reg.open_session(self.alice)
        with self.assertRaises(ValueError):
            self.reg.open_session(SessionIdentity("1.1.1.1", 1, "anon"))


class LedgerTests(unittest.TestCase):
    def test_credit_accounting(self) -> None:
        ledger = AckLedger(window=8)
        for _ in range(8):
            ledger.admit(10)
        self.assertFalse(ledger.can_admit)
        with self.assertRaises(TransferProtocolError):
            ledger.admit(10)
        self.assertEqual(ledger.ack(40), 4)
        self.assertEqual(ledger.ack(40), 0)
        self.assertEqual(ledger.outstanding, 4)
        self.assertEqual(ledger.ack(80), 4)
        with self.assertRaises(TransferProtocolError):
            ledger.ack(81)


# ------------------------------------------------------------ end to end (no sockets)


def run_transfer(testcase: unittest.TestCase, data: bytes, directory: Path, name: str = "f.bin"):
    """Drive sender -> registry -> receiver -> registry -> sender with deferred ACK delivery."""
    reg = TransferRegistry()
    alice, bob = ident("alice"), ident("bob")
    reg.open_session(alice)
    reg.open_session(bob)
    tid = str(uuid.uuid4())
    sha = hashlib.sha256(data).hexdigest()
    reg.offer(alice, bob, offer_msg(tid, "bob", len(data), sha, name))
    out = OutgoingTransfer(tid, len(data))
    sink = DiskSink(directory, name)
    inc = IncomingTransfer(tid, name, len(data), sha, sink)
    reg.handle(bob, msg("FILE_ACCEPT", transfer_id=tid))
    out.on_accept()

    to_receiver: list[Envelope] = []
    ack_frames: list[int] = []
    max_outstanding = 0
    offset = 0
    end_sent = False
    result = None
    while not out.finished:
        while out.wait_for_credit(0) and not out.all_sent and not out.finished:
            piece = data[offset : offset + CHUNK]
            offset += len(piece)
            payload = out.next_chunk_payload(piece)
            max_outstanding = max(max_outstanding, out._ledger.outstanding)
            res = reg.handle(alice, Envelope("FILE_CHUNK", "c", payload))
            testcase.assertIsNone(res.error)
            to_receiver += [f.message for f in res.forwards]
        if out.all_sent and not end_sent:
            end_sent = True
            res = reg.handle(alice, Envelope("FILE_END", "e", out.end_payload()))
            testcase.assertIsNone(res.error)
            to_receiver += [f.message for f in res.forwards]
        while to_receiver:  # receiver drains in FIFO order
            m = to_receiver.pop(0)
            if m.type == "FILE_CHUNK":
                for ack in inc.on_chunk(m.payload):
                    ack_frames.append(ack)
                    r = reg.handle(bob, msg("FILE_ACK", transfer_id=tid, next_offset=ack))
                    testcase.assertIsNone(r.error)
                    for f in r.forwards:
                        out.on_ack(f.message.payload["next_offset"])
            elif m.type == "FILE_END":
                result = inc.on_end(m.payload["total_chunks"])
                for ack in result.acks:
                    ack_frames.append(ack)
                    r = reg.handle(bob, msg("FILE_ACK", transfer_id=tid, next_offset=ack))
                    testcase.assertIsNone(r.error)
                    for f in r.forwards:
                        out.on_ack(f.message.payload["next_offset"])
                kw: dict[str, Any] = {"ok": result.ok}
                if result.reason:
                    kw["reason"] = result.reason
                r = reg.handle(bob, msg("FILE_RESULT", transfer_id=tid, **kw))
                testcase.assertIsNone(r.error)
                for f in r.forwards:
                    out.on_result(f.message.payload["ok"], f.message.payload.get("reason"))
    return reg, out, result, ack_frames, max_outstanding, bob, tid


class EndToEndTests(unittest.TestCase):
    def test_sizes_around_every_boundary(self) -> None:
        rng = random.Random(7)
        sizes = [
            0,
            1,
            5,
            CHUNK - 1,
            CHUNK,
            CHUNK + 1,
            3 * CHUNK,
            4 * CHUNK,
            4 * CHUNK + 1,
            8 * CHUNK,
            9 * CHUNK,
            9 * CHUNK + 123,
            40 * CHUNK + 7,
        ]
        for size in sizes:
            with self.subTest(size=size), tempfile.TemporaryDirectory() as tmp:
                data = rng.randbytes(size)
                reg, out, result, acks, peak, bob, tid = run_transfer(self, data, Path(tmp))
                self.assertIs(out.state, TransferState.DONE)
                assert result is not None
                assert result.saved_to is not None
                self.assertTrue(result.ok)
                self.assertEqual(Path(result.saved_to).read_bytes(), data)
                self.assertEqual(out.acked_offset, size)
                self.assertLessEqual(peak, 8)
                chunks = -(-size // CHUNK)
                expected = max(1, -(-chunks // 4)) if size else 1
                self.assertLessEqual(len(acks), expected + 1)
                self.assertEqual(acks[-1], size)
                self.assertIsNone(reg.state_of(tid))
                self.assertEqual(
                    [p.name for p in Path(tmp).iterdir() if p.name.endswith(".part")], []
                )
                again = reg.offer(ident("alice"), bob, offer_msg(str(uuid.uuid4()), "bob", 1))
                self.assertIsNone(again.error)  # receiver capacity released exactly once

    def test_window_actually_fills_to_eight_when_acks_are_delayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, _, _, peak, _, _ = run_transfer(self, os.urandom(20 * CHUNK), Path(tmp))
        self.assertEqual(peak, 8)

    def test_hash_mismatch_fails_and_publishes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = DiskSink(tmp, "x.bin")
            inc = IncomingTransfer(
                str(uuid.uuid4()), "x.bin", 3, hashlib.sha256(b"abc").hexdigest(), sink
            )
            inc.on_chunk({"seq": 0, "offset": 0, "data_b64": b64(b"abd")})
            result = inc.on_end(1)
            self.assertEqual((result.ok, result.reason), (False, "HASH_MISMATCH"))
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_receiver_detects_size_count_order_and_overrun(self) -> None:
        sha = hashlib.sha256(b"abcdef").hexdigest()

        def fresh(tmp: str) -> IncomingTransfer:
            return IncomingTransfer("t", "x", 6, sha, DiskSink(tmp, "x"))

        with tempfile.TemporaryDirectory() as tmp:
            inc = fresh(tmp)
            inc.on_chunk({"seq": 0, "offset": 0, "data_b64": b64(b"abc")})
            self.assertEqual(inc.on_end(1).reason, "SIZE_MISMATCH")
            inc = fresh(tmp)
            inc.on_chunk({"seq": 0, "offset": 0, "data_b64": b64(b"abc")})
            inc.on_chunk({"seq": 1, "offset": 3, "data_b64": b64(b"def")})
            self.assertEqual(inc.on_end(5).reason, "CHUNK_COUNT_MISMATCH")
            inc = fresh(tmp)
            with self.assertRaises(TransferProtocolError):
                inc.on_chunk({"seq": 1, "offset": 0, "data_b64": b64(b"abc")})
            self.assertTrue(inc.finished)
            inc = fresh(tmp)
            with self.assertRaises(TransferProtocolError):
                inc.on_chunk({"seq": 0, "offset": 0, "data_b64": b64(b"abcdefg")})
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_late_frames_for_a_finished_transfer_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            inc = IncomingTransfer(
                "t", "x", 1, hashlib.sha256(b"a").hexdigest(), DiskSink(tmp, "x")
            )
            inc.cancel()
            inc.cancel()
            self.assertEqual(inc.on_chunk({"seq": 0, "offset": 0, "data_b64": b64(b"a")}), ())
            self.assertFalse(inc.on_end(1).ok)
            self.assertEqual(list(Path(tmp).iterdir()), [])


class OutgoingTests(unittest.TestCase):
    def test_progress_is_receiver_confirmed_and_success_needs_ok_result(self) -> None:
        out = OutgoingTransfer("t", 3 * CHUNK)
        out.on_accept()
        for _ in range(3):
            out.next_chunk_payload(b"x" * CHUNK)
        self.assertEqual((out.sent_bytes, out.acked_offset), (3 * CHUNK, 0))
        self.assertTrue(out.all_sent)
        self.assertEqual(out.end_payload()["total_chunks"], 3)
        out.on_ack(3 * CHUNK)
        self.assertNotEqual(out.state, TransferState.DONE)  # 100% progress is not success
        out.on_result(True)
        self.assertIs(out.state, TransferState.DONE)

    def test_result_before_final_ack_or_bad_ack_fails(self) -> None:
        out = OutgoingTransfer("t", 2 * CHUNK)
        out.on_accept()
        out.next_chunk_payload(b"x" * CHUNK)
        out.next_chunk_payload(b"x" * CHUNK)
        out.on_result(True)
        self.assertIs(out.state, TransferState.FAILED)
        out = OutgoingTransfer("t", CHUNK)
        out.on_accept()
        out.next_chunk_payload(b"x" * CHUNK)
        with self.assertRaises(TransferProtocolError):
            out.on_ack(7)
        self.assertIs(out.state, TransferState.FAILED)

    def test_cannot_exceed_window_size_or_chunk_limits(self) -> None:
        out = OutgoingTransfer("t", 20 * CHUNK)
        out.on_accept()
        for _ in range(8):
            out.next_chunk_payload(b"x" * CHUNK)
        self.assertFalse(out.wait_for_credit(0))
        with self.assertRaises(TransferProtocolError):
            out.next_chunk_payload(b"x" * CHUNK)
        out2 = OutgoingTransfer("t", 10)
        out2.on_accept()
        for bad in (b"", b"x" * 11, b"x" * (CHUNK + 1)):
            with self.assertRaises(TransferProtocolError):
                out2.next_chunk_payload(bad)
        with self.assertRaises(TransferProtocolError):
            out2.end_payload()
        with self.assertRaises(ValueError):
            OutgoingTransfer("t", 50 * 1024 * 1024 + 1)

    def test_wait_for_credit_wakes_on_ack_and_on_cancel(self) -> None:
        out = OutgoingTransfer("t", 20 * CHUNK)
        out.on_accept()
        for _ in range(8):
            out.next_chunk_payload(b"x" * CHUNK)
        woke: list[bool] = []
        thread = threading.Thread(target=lambda: woke.append(out.wait_for_credit(5)))
        thread.start()
        out.on_ack(4 * CHUNK)
        thread.join(2)
        self.assertEqual(woke, [True])
        for _ in range(4):
            out.next_chunk_payload(b"x" * CHUNK)
        thread = threading.Thread(target=lambda: woke.append(out.wait_for_credit(5)))
        thread.start()
        out.on_cancel("USER_ABORT")
        thread.join(2)
        self.assertEqual(woke, [True, True])
        self.assertTrue(out.finished)


class DiskSinkAndFilenameTests(unittest.TestCase):
    def test_never_overwrites_and_picks_unique_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            names = []
            for i in range(3):
                sink = DiskSink(tmp, "report.txt")
                sink.write(f"v{i}".encode())
                sink.flush()
                names.append(Path(sink.commit()).name)
            self.assertEqual(names, ["report.txt", "report (1).txt", "report (2).txt"])
            self.assertEqual((Path(tmp) / "report.txt").read_text(), "v0")
            self.assertFalse([p for p in Path(tmp).iterdir() if p.suffix == ".part"])

    def test_hostile_names_stay_inside_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for wire in ("../../etc/passwd", "..\\..\\evil.exe", "/abs/path.txt", "CON", "nul.txt"):
                sink = DiskSink(tmp, wire)
                sink.write(b"x")
                saved = Path(sink.commit()).resolve()
                self.assertEqual(saved.parent, Path(tmp).resolve(), wire)

    def test_abort_removes_partial_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = DiskSink(tmp, "a.bin")
            sink.write(b"partial")
            sink.abort()
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_sanitize_filename(self) -> None:
        cases = {
            "a.txt": "a.txt",
            "../../x.txt": "x.txt",
            "C:\\dir\\y.txt": "y.txt",
            'bad<>:"|?*name.txt': "bad_______name.txt",
            "trailing. . ": "trailing",
            "..": "file",
            "": "file",
            "   ": "file",
            "CON": "_CON",
            "con.txt": "_con.txt",
            "Lpt9.log": "_Lpt9.log",
            "COM10": "COM10",
            "nul ": "_nul",
            "tab\there": "tab_here",
            ".hidden": "hidden",
            "e\u0301.txt": "\u00e9.txt",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(sanitize_filename(raw), expected)
        long_name = "é" * 200 + ".tar.gz"
        result = sanitize_filename(long_name)
        self.assertLessEqual(len(result.encode()), 240)
        self.assertTrue(result.endswith(".gz"))
        self.assertTrue(sanitize_filename("\ud800x.txt").endswith("x.txt"))


if __name__ == "__main__":
    unittest.main()
