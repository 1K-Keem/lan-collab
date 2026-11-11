"""Bounded, fair per-connection mailboxes; only the writer touches socket.send."""

from collections import deque
from threading import Condition


class OutboundQueue:
    def __init__(self, control_limit=128, file_limit=32):
        self.control_limit = control_limit
        self.file_limit = file_limit
        self.control = deque()
        self.file = deque()
        self._condition = Condition()
        self._controls = 0
        self.closing = False

    def put(self, message):
        with self._condition:
            if self.closing:
                return False
            is_file = message.type in {"FILE_CHUNK", "FILE_END"}
            queue = self.file if is_file else self.control
            limit = self.file_limit if is_file else self.control_limit
            if len(queue) >= limit:
                return False
            queue.append(message)
            self._condition.notify()
            return True

    def get(self):
        with self._condition:
            self._condition.wait_for(lambda: self.closing or self.control or self.file)
            if self.file and (not self.control or self._controls >= 4):
                self._controls = 0
                return self.file.popleft()
            if self.control:
                self._controls = min(4, self._controls + 1)
                return self.control.popleft()
            return None

    def purge(self, transfer_ids):
        with self._condition:
            self.file = deque(
                m for m in self.file if m.payload.get("transfer_id") not in transfer_ids
            )

    def close(self, *, discard=False):
        with self._condition:
            self.closing = True
            if discard:
                self.control.clear()
                self.file.clear()
            self._condition.notify_all()
