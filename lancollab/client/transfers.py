"""TV3 client-side file I/O, consent and receiver-confirmed progress.

TV2 owns shared transfer-core validation; TV1 owns server relay/queues.
This module uses Envelope payloads through an injected sender, never sockets.
"""

import base64
from collections import deque
from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re
import tempfile
from threading import Condition, RLock, Thread
from typing import Any, Callable
from unicodedata import category
from uuid import UUID, uuid4

from lancollab.common.contracts import (
    FILE_ACK_INTERVAL_CHUNKS,
    FILE_CHUNK_BYTES,
    FILE_WINDOW_CHUNKS,
)

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TRANSFERS_PER_SESSION = 1024
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,32}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED = re.compile(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?\Z")


class TransferError(ValueError):
    """Invalid client-side file operation or received transfer data."""


def checked_filename(name: object) -> str:
    """Reject dangerous names rather than attempting lossy sanitization."""
    if not isinstance(name, str) or not 1 <= len(name.encode("utf-8")) <= 255:
        raise TransferError("Invalid filename length")
    if (
        name in {".", ".."}
        or "/" in name
        or "\\" in name
        or ":" in name
        or name[-1] in ". "
        or any(category(c) in {"Cc", "Cf", "Cs"} for c in name)
        or _WINDOWS_RESERVED.fullmatch(name)
    ):
        raise TransferError("Unsafe filename")
    return name


def checked_id(value: object) -> str:
    if not isinstance(value, str):
        raise TransferError("Invalid transfer UUID")
    try:
        if str(UUID(value)) != value:
            raise ValueError("Noncanonical UUID")
    except ValueError as error:
        raise TransferError("Invalid transfer UUID") from error
    return value


def checked_metadata(payload: dict[str, Any]) -> tuple[str, str, int, str]:
    if set(payload) != {"transfer_id", "from", "name", "size", "sha256"}:
        raise TransferError("Invalid incoming FILE_OFFER fields")
    transfer_id = checked_id(payload["transfer_id"])
    sender = payload["from"]
    if not isinstance(sender, str) or _NAME.fullmatch(sender) is None:
        raise TransferError("Invalid sender username")
    name = checked_filename(payload["name"])
    size = payload["size"]
    digest = payload["sha256"]
    if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
        raise TransferError("Invalid file size")
    if not isinstance(digest, str) or _SHA.fullmatch(digest) is None:
        raise TransferError("Invalid SHA-256 digest")
    return transfer_id, name, size, digest


@dataclass
class Outgoing:
    path: Path
    size: int
    sha256: str
    recipient: str
    sent_bytes: int = 0
    ack_bytes: int = 0
    next_seq: int = 0
    end_sent: bool = False
    started: bool = False
    outstanding: deque[int] = field(default_factory=deque)
    error: str | None = None


@dataclass
class Incoming:
    name: str
    sender: str
    size: int
    sha256: str
    temp_path: Path | None = None
    final_path: Path | None = None
    handle: Any = None
    hasher: Any = field(default_factory=hashlib.sha256)
    written: int = 0
    chunks: int = 0
    since_ack: int = 0


class TransferManager:
    """One session's client transfer data; server and TV2 remain external.

    External callbacks are called with no manager lock held. All transfer IDs
    are terminal-tombstoned until a new session/manager is constructed.
    """

    def __init__(
        self,
        send: Callable[[str, dict[str, Any]], None],
        downloads_dir: Path | str,
        *,
        notify: Callable[[str], None] | None = None,
    ) -> None:
        self._send = send
        self._notify = notify or (lambda _text: None)
        self.downloads_dir = Path(downloads_dir)
        self._cv = Condition(RLock())
        self.outgoing: dict[str, Outgoing] = {}
        self.incoming: dict[str, Incoming] = {}
        self.terminal: set[str] = set()
        self._accepted_offers = 0
        self._closed = False

    def _announce(self, message: str) -> None:
        self._notify(message)

    def _new_offer_capacity(self, transfer_id: str) -> None:
        if self._closed:
            raise TransferError("Client session closed")
        if self._accepted_offers >= MAX_TRANSFERS_PER_SESSION:
            raise TransferError("Session transfer ID limit reached; reconnect required")
        if (
            transfer_id in self.outgoing
            or transfer_id in self.incoming
            or transfer_id in self.terminal
        ):
            raise TransferError("Transfer UUID already used in this session")

    def offer(self, path: str | Path, recipient: str) -> str:
        """Hash file locally before advertising; stream data only after consent."""
        if not isinstance(recipient, str) or _NAME.fullmatch(recipient) is None:
            raise TransferError("Invalid recipient")
        file_path = Path(path)
        checked_filename(file_path.name)
        if not file_path.is_file():
            raise TransferError("File does not exist")
        size = file_path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise TransferError("File exceeds 50 MiB")
        digest = hashlib.sha256()
        with file_path.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
        if file_path.stat().st_size != size:
            raise TransferError("File changed during hashing")
        transfer_id = str(uuid4())
        with self._cv:
            self._new_offer_capacity(transfer_id)
            self.outgoing[transfer_id] = Outgoing(file_path, size, digest.hexdigest(), recipient)
            self._accepted_offers += 1
        try:
            self._send(
                "FILE_OFFER",
                {
                    "transfer_id": transfer_id,
                    "to": recipient,
                    "name": file_path.name,
                    "size": size,
                    "sha256": digest.hexdigest(),
                },
            )
        except Exception:
            self._finish(transfer_id)
            raise
        return transfer_id

    def on_offer(self, payload: dict[str, Any]) -> None:
        transfer_id, name, size, digest = checked_metadata(payload)
        with self._cv:
            self._new_offer_capacity(transfer_id)
            if self.incoming:
                # Server should enforce one incoming receiver. Protect client too.
                reject = True
            else:
                self.incoming[transfer_id] = Incoming(name, payload["from"], size, digest)
                self._accepted_offers += 1
                reject = False
        if reject:
            self._send("FILE_REJECT", {"transfer_id": transfer_id, "reason": "RECIPIENT_BUSY"})
            return
        self._announce(
            f"Incoming file from {payload['from']}: {name} ({size} bytes); /accept {transfer_id}"
        )

    def accept(self, transfer_id: str) -> None:
        checked_id(transfer_id)
        with self._cv:
            entry = self.incoming.get(transfer_id)
            if entry is None or entry.handle is not None:
                raise TransferError("No pending file offer")
            self.downloads_dir.mkdir(parents=True, exist_ok=True)
            # Reserve a unique filename by choosing candidates, then link atomically on success.
            base = Path(entry.name)
            candidate = self.downloads_dir / base.name
            number = 1
            while candidate.exists():
                candidate = self.downloads_dir / f"{base.stem} ({number}){base.suffix}"
                number += 1
            fd, temp_name = tempfile.mkstemp(
                prefix=".lancollab-", suffix=".part", dir=self.downloads_dir
            )
            entry.handle = os.fdopen(fd, "wb")
            entry.temp_path = Path(temp_name)
            entry.final_path = candidate
        try:
            self._send("FILE_ACCEPT", {"transfer_id": transfer_id})
        except Exception:
            self._finish(transfer_id)
            raise

    def reject(self, transfer_id: str, reason: str = "DECLINED") -> None:
        checked_id(transfer_id)
        with self._cv:
            if transfer_id not in self.incoming:
                raise TransferError("No incoming offer")
        try:
            self._send("FILE_REJECT", {"transfer_id": transfer_id, "reason": reason})
        finally:
            self._finish(transfer_id)

    def cancel(self, transfer_id: str, reason: str = "USER_CANCELLED") -> None:
        checked_id(transfer_id)
        with self._cv:
            if transfer_id not in self.incoming and transfer_id not in self.outgoing:
                raise TransferError("Unknown active transfer")
        try:
            self._send("FILE_CANCEL", {"transfer_id": transfer_id, "reason": reason})
        finally:
            self._finish(transfer_id)

    def _finish(self, transfer_id: str) -> None:
        with self._cv:
            outgoing = self.outgoing.pop(transfer_id, None)
            incoming = self.incoming.pop(transfer_id, None)
            self.terminal.add(transfer_id)
            if outgoing is not None:
                outgoing.error = outgoing.error or "transfer terminated"
            self._cv.notify_all()
        if incoming is not None:
            if incoming.handle is not None:
                incoming.handle.close()
            if incoming.temp_path is not None:
                incoming.temp_path.unlink(missing_ok=True)

    def on_accept(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"transfer_id"}:
            raise TransferError("Invalid FILE_ACCEPT")
        transfer_id = checked_id(payload["transfer_id"])
        with self._cv:
            if transfer_id in self.terminal:
                return
            if transfer_id not in self.outgoing:
                raise TransferError("No matching outgoing offer")
            if self.outgoing[transfer_id].started:
                return
            self.outgoing[transfer_id].started = True
        Thread(
            target=self._stream, args=(transfer_id,), daemon=True, name="lancollab-file-sender"
        ).start()

    def _stream(self, transfer_id: str) -> None:
        """Only this worker waits on receiver ACK credit; input and receive loops never do."""
        try:
            with self._cv:
                entry = self.outgoing.get(transfer_id)
                if entry is None:
                    return
            with entry.path.open("rb") as source:
                while True:
                    with self._cv:
                        while (
                            transfer_id in self.outgoing
                            and len(entry.outstanding) >= FILE_WINDOW_CHUNKS
                        ):
                            self._cv.wait(timeout=0.25)
                        if transfer_id not in self.outgoing or self._closed:
                            return
                        offset, seq = entry.sent_bytes, entry.next_seq
                        if offset >= entry.size:
                            break
                    data = source.read(min(FILE_CHUNK_BYTES, entry.size - offset))
                    if not data:
                        raise TransferError("File changed while streaming")
                    # Reserve credit before sending so a fast ACK cannot outrun our accounting.
                    with self._cv:
                        if transfer_id not in self.outgoing:
                            return
                        entry.sent_bytes += len(data)
                        entry.next_seq += 1
                        entry.outstanding.append(entry.sent_bytes)
                    self._send(
                        "FILE_CHUNK",
                        {
                            "transfer_id": transfer_id,
                            "seq": seq,
                            "offset": offset,
                            "data_b64": base64.b64encode(data).decode("ascii"),
                        },
                    )
                if entry.path.stat().st_size != entry.size:
                    raise TransferError("File changed while streaming")
                with self._cv:
                    if transfer_id not in self.outgoing:
                        return
                    entry.end_sent = True
                # END is sent after all CHUNK frames; not after all ACKs drain.
                self._send("FILE_END", {"transfer_id": transfer_id, "total_chunks": entry.next_seq})
        except Exception as error:
            if transfer_id not in self.terminal:
                try:
                    self._send(
                        "FILE_CANCEL", {"transfer_id": transfer_id, "reason": "SENDER_IO_ERROR"}
                    )
                except Exception:
                    pass
                self._announce(f"File sender error: {error}")
                self._finish(transfer_id)

    def on_ack(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"transfer_id", "next_offset"}:
            raise TransferError("Invalid FILE_ACK")
        transfer_id = checked_id(payload["transfer_id"])
        offset = payload["next_offset"]
        if type(offset) is not int or offset < 0:
            raise TransferError("Invalid ACK offset")
        with self._cv:
            entry = self.outgoing.get(transfer_id)
            if entry is None:
                if transfer_id in self.terminal:
                    return
                raise TransferError("Unknown transfer ACK")
            if offset < entry.ack_bytes or offset > entry.sent_bytes:
                raise TransferError("Nonmonotonic or unsent ACK")
            if offset == entry.ack_bytes:
                return
            if offset not in entry.outstanding:
                raise TransferError("ACK not on a chunk-end boundary")
            while entry.outstanding and entry.outstanding[0] <= offset:
                entry.outstanding.popleft()
            entry.ack_bytes = offset
            size = entry.size
            self._cv.notify_all()
        self._announce(f"File receiver-confirmed: {offset}/{size} bytes (not yet verified)")

    def on_chunk(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"transfer_id", "seq", "offset", "data_b64"}:
            raise TransferError("Invalid FILE_CHUNK fields")
        transfer_id = checked_id(payload["transfer_id"])
        seq, offset, encoded = payload["seq"], payload["offset"], payload["data_b64"]
        if type(seq) is not int or type(offset) is not int or not isinstance(encoded, str):
            raise TransferError("Invalid chunk types")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as error:
            raise TransferError("Invalid Base64 file chunk") from error
        with self._cv:
            entry = self.incoming.get(transfer_id)
            if entry is None:
                if transfer_id in self.terminal:
                    return
                raise TransferError("Unknown incoming transfer")
            if entry.handle is None:
                raise TransferError("FILE_CHUNK before consent")
            if seq != entry.chunks or offset != entry.written:
                raise TransferError("Out-of-order incoming chunk")
            if not 1 <= len(data) <= FILE_CHUNK_BYTES or offset + len(data) > entry.size:
                raise TransferError("Invalid incoming chunk length")
            entry.handle.write(data)
            entry.hasher.update(data)
            entry.written += len(data)
            entry.chunks += 1
            entry.since_ack += 1
            acknowledge = entry.since_ack >= FILE_ACK_INTERVAL_CHUNKS or entry.written == entry.size
            if acknowledge:
                entry.handle.flush()
                entry.since_ack = 0
            ack_offset = entry.written
        if acknowledge:
            self._send("FILE_ACK", {"transfer_id": transfer_id, "next_offset": ack_offset})

    def on_end(self, payload: dict[str, Any]) -> None:
        if set(payload) != {"transfer_id", "total_chunks"}:
            raise TransferError("Invalid FILE_END")
        transfer_id = checked_id(payload["transfer_id"])
        count = payload["total_chunks"]
        if type(count) is not int or count < 0:
            raise TransferError("Invalid chunk count")
        with self._cv:
            entry = self.incoming.get(transfer_id)
            if entry is None:
                if transfer_id in self.terminal:
                    return
                raise TransferError("Unknown incoming transfer")
            if entry.handle is None:
                raise TransferError("FILE_END before consent")
            ok = (
                count == entry.chunks
                and entry.written == entry.size
                and entry.hasher.hexdigest() == entry.sha256
            )
            # Flush final data and close temp file before committing to final path.
            entry.handle.flush()
            entry.handle.close()
            entry.handle = None
            path = entry.final_path
            temp = entry.temp_path
            size = entry.size
        # Zero-byte file has no chunks: final ACK=0 must precede RESULT.
        if size == 0:
            self._send("FILE_ACK", {"transfer_id": transfer_id, "next_offset": 0})
        if ok and path is not None and temp is not None:
            try:
                # Atomic, no-clobber commit (hardlink fails if target already exists).
                os.link(temp, path)
                temp.unlink()
            except OSError:
                ok = False
        try:
            result = {"transfer_id": transfer_id, "ok": ok}
            if not ok:
                result["reason"] = "SIZE_HASH_OR_SAVE_FAILED"
            self._send("FILE_RESULT", result)
        finally:
            self._announce(f"File {'saved' if ok else 'verification failed'}: {entry.name}")
            self._finish(transfer_id)

    def on_terminal(self, kind: str, payload: dict[str, Any]) -> None:
        transfer_id = checked_id(payload.get("transfer_id"))
        if kind == "FILE_RESULT":
            if type(payload.get("ok")) is not bool or not set(payload) <= {
                "transfer_id",
                "ok",
                "reason",
            }:
                raise TransferError("Invalid FILE_RESULT")
            with self._cv:
                entry = self.outgoing.get(transfer_id)
                if entry is None:
                    if transfer_id in self.terminal:
                        return
                    raise TransferError("Unexpected FILE_RESULT")
                if payload["ok"] and (not entry.end_sent or entry.ack_bytes != entry.size):
                    raise TransferError("Premature success FILE_RESULT")
            message = (
                "File transfer verified successfully" if payload["ok"] else "File transfer failed"
            )
        elif kind == "FILE_REJECT":
            if not set(payload) <= {"transfer_id", "reason"}:
                raise TransferError("Invalid FILE_REJECT")
            message = "File offer declined"
        elif kind == "FILE_CANCEL":
            if set(payload) != {"transfer_id", "reason"} or not isinstance(payload["reason"], str):
                raise TransferError("Invalid FILE_CANCEL")
            message = "File transfer cancelled"
        else:
            raise TransferError("Unknown terminal file event")
        with self._cv:
            if transfer_id in self.terminal:
                return
            if transfer_id not in self.outgoing and transfer_id not in self.incoming:
                raise TransferError("Unexpected file terminal event")
        self._finish(transfer_id)
        self._announce(message)

    def handle_event(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "FILE_OFFER":
            self.on_offer(payload)
        elif kind == "FILE_ACCEPT":
            self.on_accept(payload)
        elif kind == "FILE_CHUNK":
            self.on_chunk(payload)
        elif kind == "FILE_END":
            self.on_end(payload)
        elif kind == "FILE_ACK":
            self.on_ack(payload)
        elif kind in {"FILE_REJECT", "FILE_CANCEL", "FILE_RESULT"}:
            self.on_terminal(kind, payload)
        else:
            raise TransferError("Unsupported file event")

    def abort(self, transfer_id: str, reason: str) -> None:
        """Reject a transfer locally after a correlated server ERROR, without sending CANCEL."""
        with self._cv:
            if transfer_id in self.terminal:
                return
            exists = transfer_id in self.outgoing or transfer_id in self.incoming
        if exists:
            self._finish(transfer_id)
            self._announce(f"Transfer aborted: {reason}")

    def close(self) -> None:
        with self._cv:
            self._closed = True
            ids = list(self.outgoing) + list(self.incoming)
            self._cv.notify_all()
        for transfer_id in ids:
            self._finish(transfer_id)
