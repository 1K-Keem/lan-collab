"""Socket-independent file-transfer core (issue #8, protocol-spec sections 4 and 5).

Nothing here performs network I/O or blocks. The pieces:

* :class:`AckLedger` -- shared W=8 credit/ACK-boundary accounting used on both ends.
* :class:`TransferRegistry` -- the *server's* authorization and state machine. The server feeds it
  authenticated messages and enqueues the returned :class:`Forward` actions after releasing
  its own locks.
* :class:`OutgoingTransfer` -- the sender's state (credit window, receiver-confirmed progress).
* :class:`IncomingTransfer` -- the receiver's validation, ACK batching and verification, with a
  pluggable :class:`ChunkSink` (:class:`DiskSink` is the default disk implementation).

Success is only ever ``FILE_RESULT(ok=true)``; ACK progress is "bytes written", not "verified".
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from .codec import decoded_b64_length
from .contracts import Envelope, FILE_ACK_INTERVAL_CHUNKS, FILE_CHUNK_BYTES, SessionIdentity
from .filenames import sanitize_filename
from .message_schema import FIELD_LIMITS

WINDOW_CHUNKS = FIELD_LIMITS["max_file_window_chunks"]
MAX_FILE_BYTES = FIELD_LIMITS["file_size_bytes"]
MAX_SEEN_TRANSFER_IDS = FIELD_LIMITS["max_seen_transfer_ids_per_session"]

OFFER_TIMEOUT = 60.0
FIRST_DATA_TIMEOUT = 10.0
STALL_TIMEOUT = 15.0
VERIFY_TIMEOUT = 30.0

Lane = Literal["control", "file"]


class TransferProtocolError(Exception):
    """A rule of the transfer protocol was violated; ``code`` is a wire error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class TransferState(Enum):
    OFFERED = "OFFERED"
    ACCEPTED = "ACCEPTED"
    TRANSFERRING = "TRANSFERRING"
    VERIFYING = "VERIFYING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (TransferState.DONE, TransferState.FAILED, TransferState.CANCELLED)


# ------------------------------------------------------------------------------ ledger


class AckLedger:
    """Chunk-credit accounting: at most ``window`` chunks sent but not receiver-acknowledged.

    ACKs are cumulative and may only advance to an actual chunk-end boundary. Equal-offset
    duplicates release nothing; decreasing, non-boundary or oversized offsets are violations.
    """

    def __init__(self, window: int = WINDOW_CHUNKS) -> None:
        self.window = window
        self.sent_bytes = 0
        self.acked_offset = 0
        self._chunk_ends: deque[int] = deque()

    @property
    def outstanding(self) -> int:
        return len(self._chunk_ends)

    @property
    def can_admit(self) -> bool:
        return len(self._chunk_ends) < self.window

    def admit(self, length: int) -> None:
        if not self.can_admit:
            raise TransferProtocolError("INVALID_STATE", "chunk outside the credit window")
        self.sent_bytes += length
        self._chunk_ends.append(self.sent_bytes)

    def ack(self, next_offset: int) -> int:
        """Apply a cumulative ACK; returns the number of chunk credits released."""
        if next_offset < self.acked_offset or next_offset > self.sent_bytes:
            raise TransferProtocolError("INVALID_STATE", "ACK offset out of range")
        if next_offset == self.acked_offset:
            return 0
        if next_offset not in self._chunk_ends:
            raise TransferProtocolError("INVALID_STATE", "ACK offset is not a chunk boundary")
        released = 0
        while self._chunk_ends and self._chunk_ends[0] <= next_offset:
            self._chunk_ends.popleft()
            released += 1
        self.acked_offset = next_offset
        return released


# --------------------------------------------------------------------- server registry


@dataclass(frozen=True)
class Forward:
    """An event the server must enqueue for ``recipient`` (a concrete session, never a username)."""

    recipient: SessionIdentity
    message: Envelope
    lane: Lane


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    message: str

    def reply(self, request_id: str) -> Envelope:
        """Direct ``ERROR`` response for the request that was rejected."""
        return Envelope("ERROR", request_id, {"code": self.code, "message": self.message})


@dataclass(frozen=True)
class Outcome:
    """Result of one registry call. All fields are plain data; nothing has been sent."""

    forwards: tuple[Forward, ...] = ()
    error: ErrorInfo | None = None
    purge: tuple[str, ...] = ()  # drop queued FILE_CHUNK/FILE_END frames of these transfer ids

    def merged(self, other: Outcome) -> Outcome:
        return Outcome(
            self.forwards + other.forwards,
            self.error or other.error,
            self.purge + other.purge,
        )


@dataclass
class _Transfer:
    transfer_id: str
    sender: SessionIdentity
    receiver: SessionIdentity
    name: str
    size: int
    sha256: str
    ledger: AckLedger
    state: TransferState = TransferState.OFFERED
    admitted_chunks: int = 0
    next_seq: int = 0
    final_ack_seen: bool = False
    end_written: bool = False
    deadline: float | None = None


@dataclass
class _SessionRecord:
    identity: SessionIdentity
    seen_ids: set[str] = field(default_factory=set)
    active: set[str] = field(default_factory=set)
    incoming: str | None = None


def _new_event_id() -> str:
    return "evt-" + uuid.uuid4().hex


_RECEIVER_ONLY = {"FILE_ACCEPT", "FILE_REJECT", "FILE_ACK", "FILE_RESULT"}
_SENDER_ONLY = {"FILE_CHUNK", "FILE_END"}


class TransferRegistry:
    """Server-side authorization, state and credit tracking for relayed transfers.

    Active transfers are keyed by ``transfer_id`` alone and bound to the exact sender and
    receiver ``session_id``. Every method is thread-safe, non-blocking and returns an
    :class:`Outcome`; the caller enqueues ``forwards`` (nonblocking) and drops ``purge`` ids
    from its file queue. If a file-queue put fails call :meth:`fail_queue_full`; if the control
    queue is full close the slow session and call :meth:`close_session`.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = _new_event_id,
        window: int = WINDOW_CHUNKS,
    ) -> None:
        self._clock = clock
        self._new_id = id_factory
        self._window = window
        self._lock = threading.RLock()
        self._sessions: dict[str, _SessionRecord] = {}
        self._active: dict[str, _Transfer] = {}

    # ------------------------------------------------------------------ sessions
    def open_session(self, identity: SessionIdentity) -> None:
        """Register an authenticated session (never reused; ``username`` must be set)."""
        if identity.username is None:
            raise ValueError("session must be authenticated")
        with self._lock:
            if identity.session_id in self._sessions:
                raise ValueError("session id already registered")
            self._sessions[identity.session_id] = _SessionRecord(identity)

    def close_session(self, session_id: str, reason: str = "DISCONNECTED") -> Outcome:
        """Invalidate a session (logout, EOF, timeout, takeover or slow consumer).

        Cancels every transfer it takes part in, notifying only the surviving peer, and
        forgets the session's seen-id history.
        """
        with self._lock:
            record = self._sessions.get(session_id)
            if record is None:
                return Outcome()
            result = Outcome()
            for transfer_id in sorted(record.active):
                transfer = self._active.get(transfer_id)
                if transfer is None:
                    continue
                is_sender = transfer.sender.session_id == session_id
                peer = transfer.receiver if is_sender else transfer.sender
                result = result.merged(
                    self._terminate(transfer, TransferState.FAILED, reason, notify=(peer,))
                )
            self._sessions.pop(session_id, None)
            return result

    # -------------------------------------------------------------------- offers
    def offer(
        self, sender: SessionIdentity, receiver: SessionIdentity, message: Envelope
    ) -> Outcome:
        """Admit a ``C:FILE_OFFER``. The server resolves ``payload['to']`` to ``receiver`` first."""
        payload = message.payload
        with self._lock:
            sender_rec = self._sessions.get(sender.session_id)
            if sender_rec is None:
                return _error("NOT_AUTHENTICATED", "session is not authenticated")
            receiver_rec = self._sessions.get(receiver.session_id)
            if receiver_rec is None:
                return _error("USER_OFFLINE", "recipient is not online")
            if (
                sender.session_id == receiver.session_id
                or sender.username == receiver.username
                or payload.get("to") != receiver.username
            ):
                return _error("INVALID_MESSAGE", "invalid transfer recipient")
            if payload["size"] > MAX_FILE_BYTES:
                return _error("FILE_TOO_LARGE", "file exceeds the 50 MiB limit")
            transfer_id = payload["transfer_id"]
            if (
                transfer_id in self._active
                or transfer_id in sender_rec.seen_ids
                or transfer_id in receiver_rec.seen_ids
            ):
                return _error("TRANSFER_ID_CONFLICT", "transfer id already used")
            if (
                len(sender_rec.seen_ids) >= MAX_SEEN_TRANSFER_IDS
                or len(receiver_rec.seen_ids) >= MAX_SEEN_TRANSFER_IDS
            ):
                return _error("TRANSFER_LIMIT_REACHED", "too many transfers on this session")
            if receiver_rec.incoming is not None:
                return _error("RECIPIENT_BUSY", "recipient already has an incoming transfer")

            transfer = _Transfer(
                transfer_id,
                sender_rec.identity,
                receiver_rec.identity,
                payload["name"],
                payload["size"],
                payload["sha256"],
                AckLedger(self._window),
            )
            self._active[transfer_id] = transfer
            sender_rec.seen_ids.add(transfer_id)
            receiver_rec.seen_ids.add(transfer_id)
            sender_rec.active.add(transfer_id)
            receiver_rec.active.add(transfer_id)
            receiver_rec.incoming = transfer_id
            event = Envelope(
                "FILE_OFFER",
                self._new_id(),
                {
                    "transfer_id": transfer_id,
                    "from": sender_rec.identity.username,
                    "name": transfer.name,
                    "size": transfer.size,
                    "sha256": transfer.sha256,
                },
            )
            return Outcome(forwards=(Forward(receiver_rec.identity, event, "control"),))

    # ------------------------------------------------------- participant messages
    def handle(self, session: SessionIdentity, message: Envelope) -> Outcome:
        """Authorize and apply a client ``FILE_*`` message (except ``FILE_OFFER``)."""
        with self._lock:
            if session.session_id not in self._sessions:
                return _error("NOT_AUTHENTICATED", "session is not authenticated")
            transfer = self._active.get(message.payload["transfer_id"])
            if transfer is None or session.session_id not in (
                transfer.sender.session_id,
                transfer.receiver.session_id,
            ):
                # Unknown, terminal or foreign UUID: knowledge of an id is not authorization.
                return _error("TRANSFER_NOT_FOUND", "no such active transfer")
            is_sender = session.session_id == transfer.sender.session_id
            kind = message.type
            if (kind in _RECEIVER_ONLY and is_sender) or (kind in _SENDER_ONLY and not is_sender):
                return _error("INVALID_STATE", "not permitted for this participant")
            handler = {
                "FILE_ACCEPT": self._on_accept,
                "FILE_REJECT": self._on_reject,
                "FILE_CHUNK": self._on_chunk,
                "FILE_END": self._on_end,
                "FILE_ACK": self._on_ack,
                "FILE_RESULT": self._on_result,
                "FILE_CANCEL": self._on_cancel,
            }.get(kind)
            if handler is None:
                return _error("INVALID_MESSAGE", "unsupported file message")
            return handler(transfer, message, is_sender)

    def _on_accept(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        if t.state is not TransferState.OFFERED:
            return _error("INVALID_STATE", "transfer is not awaiting a decision")
        t.state = TransferState.ACCEPTED
        t.deadline = None  # starts when ACCEPT is actually written to the sender
        return self._to(t.sender, "FILE_ACCEPT", {"transfer_id": t.transfer_id})

    def _on_reject(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        if t.state is not TransferState.OFFERED:
            return _error("INVALID_STATE", "transfer is not awaiting a decision")
        payload: dict[str, Any] = {"transfer_id": t.transfer_id}
        if "reason" in m.payload:
            payload["reason"] = m.payload["reason"]
        notice = self._to(t.sender, "FILE_REJECT", payload)
        return notice.merged(self._terminate(t, TransferState.CANCELLED, "REJECTED", notify=()))

    def _on_chunk(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        p = m.payload
        if t.state not in (TransferState.ACCEPTED, TransferState.TRANSFERRING):
            return self._violation(t, "INVALID_STATE", "chunk not allowed in this state")
        length = decoded_b64_length(p["data_b64"])
        if p["seq"] != t.next_seq or p["offset"] != t.ledger.sent_bytes:
            return self._violation(t, "INVALID_CHUNK_OFFSET", "unexpected chunk sequence/offset")
        if t.ledger.sent_bytes + length > t.size:
            return self._violation(t, "INVALID_CHUNK_OFFSET", "chunk exceeds advertised size")
        try:
            t.ledger.admit(length)
        except TransferProtocolError as exc:
            return self._violation(t, exc.code, exc.message)
        t.next_seq += 1
        t.admitted_chunks += 1
        t.state = TransferState.TRANSFERRING
        t.deadline = self._clock() + STALL_TIMEOUT
        event = self._event("FILE_CHUNK", dict(p))
        return Outcome(forwards=(Forward(t.receiver, event, "file"),))

    def _on_end(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        empty_start = t.state is TransferState.ACCEPTED and t.size == 0
        if not (empty_start or t.state is TransferState.TRANSFERRING):
            return self._violation(t, "INVALID_STATE", "END not allowed in this state")
        if t.ledger.sent_bytes != t.size or m.payload["total_chunks"] != t.admitted_chunks:
            return self._violation(t, "INVALID_STATE", "END does not match admitted data")
        t.state = TransferState.VERIFYING  # stall watchdog keeps running until END is written
        event = self._event("FILE_END", dict(m.payload))
        return Outcome(forwards=(Forward(t.receiver, event, "file"),))

    def _on_ack(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        offset = m.payload["next_offset"]
        allowed = t.state in (TransferState.TRANSFERRING, TransferState.VERIFYING) or (
            t.state is TransferState.ACCEPTED and t.size == 0 and offset == 0
        )
        if not allowed:
            return self._violation(t, "INVALID_STATE", "ACK not allowed in this state")
        try:
            released = t.ledger.ack(offset)
        except TransferProtocolError as exc:
            return self._violation(t, exc.code, exc.message)
        first_final = offset == t.size and not t.final_ack_seen
        if offset == t.size:
            t.final_ack_seen = True
        if not released and not first_final:
            return Outcome()  # duplicate: no credit, no timer progress, nothing to forward
        if released and t.state is TransferState.TRANSFERRING:
            t.deadline = self._clock() + STALL_TIMEOUT
        elif released and t.state is TransferState.VERIFYING and not t.end_written:
            t.deadline = self._clock() + STALL_TIMEOUT
        return self._to(t.sender, "FILE_ACK", dict(m.payload), lane="control")

    def _on_result(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        if t.state is not TransferState.VERIFYING:
            return self._violation(t, "INVALID_STATE", "premature FILE_RESULT")
        ok = m.payload["ok"]
        if ok and not t.final_ack_seen:
            return self._violation(t, "INVALID_STATE", "success requires the final ACK first")
        notice = self._to(t.sender, "FILE_RESULT", dict(m.payload))
        final = TransferState.DONE if ok else TransferState.FAILED
        return notice.merged(self._terminate(t, final, "RESULT", notify=()))

    def _on_cancel(self, t: _Transfer, m: Envelope, is_sender: bool) -> Outcome:
        peer = t.receiver if is_sender else t.sender
        return self._terminate(t, TransferState.CANCELLED, m.payload["reason"], notify=(peer,))

    # ------------------------------------------------------------- timers / failures
    def mark_written(self, transfer_id: str, message_type: str) -> None:
        """The server writer reports a completed socket write; this starts the matching deadline.

        ``FILE_OFFER`` (to receiver) starts the 60 s decision timer, ``FILE_ACCEPT`` (to
        sender) the 10 s first-data timer, ``FILE_END`` (to receiver) the 30 s verification
        timer. Enqueue time is not delivery, so these must not be started at enqueue.
        """
        with self._lock:
            t = self._active.get(transfer_id)
            if t is None:
                return
            now = self._clock()
            if message_type == "FILE_OFFER" and t.state is TransferState.OFFERED:
                t.deadline = now + OFFER_TIMEOUT
            elif message_type == "FILE_ACCEPT" and t.state is TransferState.ACCEPTED:
                t.deadline = now + FIRST_DATA_TIMEOUT
            elif (
                message_type == "FILE_END"
                and t.state is TransferState.VERIFYING
                and not t.end_written
            ):
                t.end_written = True
                t.deadline = now + VERIFY_TIMEOUT

    def expire(self) -> Outcome:
        """Fail every transfer whose deadline passed; notify both connected participants."""
        with self._lock:
            now = self._clock()
            result = Outcome()
            for t in [x for x in self._active.values() if x.deadline and now >= x.deadline]:
                result = result.merged(
                    self._terminate(t, TransferState.FAILED, "TIMEOUT", (t.sender, t.receiver))
                )
            return result

    def fail_queue_full(self, transfer_id: str) -> Outcome:
        """A nonblocking file-queue put failed: cancel only the affected transfer."""
        with self._lock:
            t = self._active.get(transfer_id)
            if t is None:
                return Outcome()
            return self._terminate(t, TransferState.FAILED, "QUEUE_FULL", (t.sender, t.receiver))

    def state_of(self, transfer_id: str) -> TransferState | None:
        with self._lock:
            t = self._active.get(transfer_id)
            return t.state if t else None

    # ------------------------------------------------------------------- internals
    def _event(self, kind: str, payload: dict[str, Any]) -> Envelope:
        return Envelope(kind, self._new_id(), payload)

    def _to(
        self, target: SessionIdentity, kind: str, payload: dict[str, Any], lane: Lane = "control"
    ) -> Outcome:
        return Outcome(forwards=(Forward(target, self._event(kind, payload), lane),))

    def _violation(self, t: _Transfer, code: str, message: str) -> Outcome:
        """Protocol violation by a participant: ERROR for the request plus abort of the transfer."""
        aborted = self._terminate(t, TransferState.FAILED, code, (t.sender, t.receiver))
        return Outcome(aborted.forwards, ErrorInfo(code, message), aborted.purge)

    def _terminate(
        self,
        t: _Transfer,
        final: TransferState,
        reason: str,
        notify: tuple[SessionIdentity, ...],
    ) -> Outcome:
        """Mark terminal exactly once, release receiver capacity and build cancel notices."""
        if t.state.terminal:
            return Outcome()
        t.state = final
        t.deadline = None
        self._active.pop(t.transfer_id, None)
        for identity in (t.sender, t.receiver):
            record = self._sessions.get(identity.session_id)
            if record is not None:
                record.active.discard(t.transfer_id)
                if record.incoming == t.transfer_id:
                    record.incoming = None
        forwards = tuple(
            Forward(
                identity,
                self._event("FILE_CANCEL", {"transfer_id": t.transfer_id, "reason": reason}),
                "control",
            )
            for identity in notify
            if identity.session_id in self._sessions
        )
        return Outcome(forwards=forwards, purge=(t.transfer_id,))


def _error(code: str, message: str) -> Outcome:
    return Outcome(error=ErrorInfo(code, message))


# --------------------------------------------------------------------------- sender


class OutgoingTransfer:
    """Sender-side state: credit window, receiver-confirmed progress, terminal outcome.

    One sender worker thread calls :meth:`wait_for_credit` / :meth:`next_chunk_payload` while
    the connection reader thread feeds :meth:`on_ack`, :meth:`on_result` and so on. The reader
    never blocks; only the worker waits for credit.
    """

    def __init__(self, transfer_id: str, size: int, *, window: int = WINDOW_CHUNKS) -> None:
        if not 0 <= size <= MAX_FILE_BYTES:
            raise ValueError("size must be between 0 and 50 MiB")
        self.transfer_id = transfer_id
        self.size = size
        self.state = TransferState.OFFERED
        self.reason: str | None = None
        self._ledger = AckLedger(window)
        self._seq = 0
        self._cond = threading.Condition()

    @property
    def acked_offset(self) -> int:
        """Receiver-confirmed bytes written (use this for progress, not bytes sent)."""
        return self._ledger.acked_offset

    @property
    def sent_bytes(self) -> int:
        return self._ledger.sent_bytes

    @property
    def all_sent(self) -> bool:
        return self._ledger.sent_bytes >= self.size

    @property
    def finished(self) -> bool:
        return self.state.terminal

    def on_accept(self) -> None:
        with self._cond:
            if self.state is TransferState.OFFERED:
                self.state = TransferState.ACCEPTED
            self._cond.notify_all()

    def wait_for_credit(self, timeout: float | None = None) -> bool:
        """Block until a chunk may be sent (or the transfer ended). ``False`` on timeout."""
        with self._cond:
            return self._cond.wait_for(self._sendable_or_done, timeout)

    def _sendable_or_done(self) -> bool:
        return self.state.terminal or self._can_send()

    def _can_send(self) -> bool:
        active = self.state in (TransferState.ACCEPTED, TransferState.TRANSFERRING)
        return active and self._ledger.can_admit and not self.all_sent

    def next_chunk_payload(self, data: bytes) -> dict[str, Any]:
        """Account for ``data`` and return the ``C:FILE_CHUNK`` payload to send."""
        with self._cond:
            if not self._can_send():
                raise TransferProtocolError("INVALID_STATE", "no credit or transfer not active")
            if not 1 <= len(data) <= FILE_CHUNK_BYTES or self.sent_bytes + len(data) > self.size:
                raise TransferProtocolError("INVALID_CHUNK_OFFSET", "invalid chunk length")
            payload = {
                "transfer_id": self.transfer_id,
                "seq": self._seq,
                "offset": self._ledger.sent_bytes,
                "data_b64": base64.b64encode(data).decode("ascii"),
            }
            self._ledger.admit(len(data))
            self._seq += 1
            self.state = TransferState.TRANSFERRING
            return payload

    def end_payload(self) -> dict[str, Any]:
        """``C:FILE_END`` payload; send as soon as all bytes are sent, without draining ACKs."""
        with self._cond:
            if not self.all_sent:
                raise TransferProtocolError("INVALID_STATE", "not all bytes were sent")
            return {"transfer_id": self.transfer_id, "total_chunks": self._seq}

    def on_ack(self, next_offset: int) -> int:
        """Apply a receiver ACK; returns credits released. Violations end the transfer."""
        with self._cond:
            try:
                released = self._ledger.ack(next_offset)
            except TransferProtocolError:
                self._finish(TransferState.FAILED, "INVALID_ACK")
                raise
            self._cond.notify_all()
            return released

    def on_result(self, ok: bool, reason: str | None = None) -> None:
        """Only ``ok=True`` after the final ACK counts as success."""
        with self._cond:
            if ok and self._ledger.acked_offset != self.size:
                self._finish(TransferState.FAILED, "RESULT_BEFORE_FINAL_ACK")
            elif ok:
                self._finish(TransferState.DONE, None)
            else:
                self._finish(TransferState.FAILED, reason or "RECEIVER_REJECTED_DATA")

    def on_reject(self, reason: str | None = None) -> None:
        with self._cond:
            self._finish(TransferState.CANCELLED, reason or "REJECTED")

    def on_cancel(self, reason: str) -> None:
        with self._cond:
            self._finish(TransferState.CANCELLED, reason)

    def _finish(self, state: TransferState, reason: str | None) -> None:
        if not self.state.terminal:
            self.state, self.reason = state, reason
        self._cond.notify_all()


# ------------------------------------------------------------------------- receiver


class ChunkSink(Protocol):
    """Where a receiver stores bytes; ``flush`` must precede the ACK that covers them."""

    def write(self, data: bytes) -> None: ...

    def flush(self) -> None: ...

    def commit(self) -> Path | str | None: ...

    def abort(self) -> None: ...


class DiskSink:
    """Temporary ``.part`` file in ``directory``, atomically published without overwriting.

    The final name is derived with :func:`sanitize_filename` and made unique (``name (1).ext``).
    """

    def __init__(self, directory: str | Path, wire_name: str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._name = sanitize_filename(wire_name)
        self._tmp = self._dir / f".lancollab-{uuid.uuid4().hex}.part"
        self._file = open(self._tmp, "xb")  # noqa: SIM115 - lifetime spans several calls

    def write(self, data: bytes) -> None:
        self._file.write(data)

    def flush(self) -> None:
        self._file.flush()  # Python buffer -> OS; crash durability (fsync) is out of scope

    def commit(self) -> Path:
        self._file.close()
        stem, dot, ext = self._name.rpartition(".")
        if not dot or not stem:
            stem, ext = self._name, ""
        else:
            ext = "." + ext
        for n in range(0, 10_000):
            candidate = self._dir / (self._name if n == 0 else f"{stem} ({n}){ext}")
            try:
                os.link(self._tmp, candidate)  # atomic, fails if the name exists
            except FileExistsError:
                continue
            except OSError:  # filesystem without hard links (e.g. FAT): rename never overwrites
                if candidate.exists():
                    continue
                try:
                    os.rename(self._tmp, candidate)
                except FileExistsError:
                    continue
                return candidate
            self._tmp.unlink(missing_ok=True)
            return candidate
        raise OSError("could not choose a unique destination name")

    def abort(self) -> None:
        try:
            self._file.close()
        finally:
            self._tmp.unlink(missing_ok=True)


@dataclass(frozen=True)
class EndResult:
    """What the receiver must send after ``FILE_END``: ACK offsets first, then the result."""

    acks: tuple[int, ...]
    ok: bool
    reason: str | None = None
    saved_to: Path | str | None = None


class IncomingTransfer:
    """Receiver-side validation, cumulative-ACK batching and verification.

    Feed accepted ``FILE_CHUNK`` payloads to :meth:`on_chunk` (it returns ACK offsets to send
    immediately: every 4 contiguous chunks and as soon as the advertised size is reached) and
    ``FILE_END`` to :meth:`on_end`. Frames for a finished transfer are ignored.
    """

    def __init__(
        self, transfer_id: str, name: str, size: int, sha256: str, sink: ChunkSink
    ) -> None:
        self.transfer_id = transfer_id
        self.name = name
        self.size = size
        self.expected_sha256 = sha256
        self.state = TransferState.TRANSFERRING
        self.written = 0
        self.chunks = 0
        self._sink = sink
        self._hash = hashlib.sha256()
        self._since_ack = 0
        self._last_ack = -1

    @property
    def finished(self) -> bool:
        return self.state.terminal

    def on_chunk(self, payload: dict[str, Any]) -> tuple[int, ...]:
        if self.finished:
            return ()
        try:
            data = base64.b64decode(payload["data_b64"], validate=True)
            if payload["seq"] != self.chunks or payload["offset"] != self.written:
                raise TransferProtocolError("INVALID_CHUNK_OFFSET", "out-of-order chunk")
            if self.written + len(data) > self.size:
                raise TransferProtocolError("INVALID_CHUNK_OFFSET", "more data than advertised")
            self._sink.write(data)
            self._sink.flush()
        except TransferProtocolError:
            self.cancel()
            raise
        except (OSError, ValueError) as exc:
            self.cancel()
            raise TransferProtocolError("TRANSFER_FAILED", "could not store chunk") from exc
        self._hash.update(data)
        self.written += len(data)
        self.chunks += 1
        self._since_ack += 1
        if self._since_ack >= FILE_ACK_INTERVAL_CHUNKS or self.written == self.size:
            return self._ack()
        return ()

    def _ack(self) -> tuple[int, ...]:
        self._since_ack = 0
        self._last_ack = self.written
        return (self.written,)

    def on_end(self, total_chunks: int) -> EndResult:
        """Verify size, chunk count and SHA-256, then publish (ok) or discard the file."""
        if self.finished:
            return EndResult((), False, "TRANSFER_NOT_ACTIVE")
        acks: tuple[int, ...] = ()
        reason: str | None = None
        if self.written != self.size:
            reason = "SIZE_MISMATCH"
        elif total_chunks != self.chunks:
            reason = "CHUNK_COUNT_MISMATCH"
        else:
            if self._last_ack != self.written:  # empty file, or a final partial not yet ACKed
                acks = self._ack()
            if self._hash.hexdigest() != self.expected_sha256:
                reason = "HASH_MISMATCH"
        if reason is None:
            try:
                saved = self._sink.commit()
            except OSError:
                self.state = TransferState.FAILED
                return EndResult(acks, False, "WRITE_FAILED")
            self.state = TransferState.DONE
            return EndResult(acks, True, None, saved)
        self._sink.abort()
        self.state = TransferState.FAILED
        return EndResult(acks, False, reason)

    def cancel(self) -> None:
        """Peer cancelled, session replaced or local abort: discard partial data once."""
        if not self.finished:
            self.state = TransferState.CANCELLED
            self._sink.abort()


def hash_file(path: str | Path) -> tuple[int, str]:
    """Return ``(size, sha256 hex)`` of a file, streaming in 64 KiB blocks."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while block := handle.read(65536):
            digest.update(block)
            size += len(block)
    return size, digest.hexdigest()
