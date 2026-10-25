"""TV3 TODO: client interaction API using an injected transport.

Start with FakeTransport in unit tests, then swap to TV2's SocketTransport.
"""

from lancollab.common.contracts import Envelope, MessageTransport


class CollabClient:
    def __init__(self, transport: MessageTransport) -> None:
        self.transport = transport

    def login(self, username: str, password: str) -> None:
        raise NotImplementedError("TV3: build AUTH_REQUEST and process AUTH_RESULT")

    def send_global(self, text: str) -> None:
        raise NotImplementedError("TV3: send CHAT_GLOBAL")

    def send_private(self, recipient: str, text: str) -> None:
        raise NotImplementedError("TV3: send CHAT_PRIVATE")

    def handle_event(self, event: Envelope) -> None:
        raise NotImplementedError("TV3: show routed messages and presence events")
