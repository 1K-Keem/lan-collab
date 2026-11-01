import time

# Protocol Constraints
WINDOW_CHUNKS = 8
CHUNK_BYTES = 16384
MAX_IN_FLIGHT_BYTES = WINDOW_CHUNKS * CHUNK_BYTES
STALL_TIMEOUT_SEC = 15.0


class FileTransferState:
    """Socket-independent core for tracking flow control and state transitions."""

    def __init__(self, transfer_id: str, sender: str, receiver: str, size: int):
        self.transfer_id = transfer_id
        self.sender = sender
        self.receiver = receiver
        self.size = size

        self.state = "OFFERED"  # OFFERED -> ACCEPTED -> TRANSFERRING -> VERIFYING -> DONE/FAILED/CANCELLED[cite: 1]
        self.sent_bytes = 0
        self.acked_bytes = 0
        self.last_progress_time = time.monotonic()

    def accept(self) -> None:
        """Transitions state to ACCEPTED[cite: 1]."""
        if self.state != "OFFERED":
            raise ValueError(f"Cannot accept transfer in state {self.state}")
        self.state = "ACCEPTED"
        self.last_progress_time = time.monotonic()

    def can_send_chunk(self) -> bool:
        """Checks if the W=8 window allows sending another chunk[cite: 1]."""
        if self.state not in ("ACCEPTED", "TRANSFERRING"):
            return False
        bytes_in_flight = self.sent_bytes - self.acked_bytes
        return bytes_in_flight < MAX_IN_FLIGHT_BYTES and self.sent_bytes < self.size

    def record_chunk_sent(self, chunk_size: int) -> None:
        """Updates outgoing byte count[cite: 1]."""
        if not self.can_send_chunk():
            raise ValueError("Window is full or transfer not active")
        self.state = "TRANSFERRING"
        self.sent_bytes += chunk_size

    def update_ack(self, next_offset: int) -> int:
        """Processes a FILE_ACK and returns the newly acknowledged bytes[cite: 1]."""
        if next_offset > self.size or next_offset < self.acked_bytes:
            raise ValueError(f"Invalid ACK offset: {next_offset}")

        new_bytes = next_offset - self.acked_bytes
        if new_bytes > 0:
            self.acked_bytes = next_offset
            self.last_progress_time = time.monotonic()  # Reset the 15s stall timer[cite: 1]

        # If all bytes are ACKed, wait for FILE_RESULT to move to DONE[cite: 1]
        return new_bytes

    def check_stall(self) -> bool:
        """Returns True if the transfer has stalled for 15 seconds[cite: 1]."""
        if self.state in ("DONE", "FAILED", "CANCELLED"):
            return False
        return (time.monotonic() - self.last_progress_time) >= STALL_TIMEOUT_SEC

    def cancel(self) -> None:
        """Marks the transfer as permanently cancelled[cite: 1]."""
        self.state = "CANCELLED"
