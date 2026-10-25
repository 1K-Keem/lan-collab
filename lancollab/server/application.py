"""TV1 TODO: server session manager, authentication and chat routing.

Design the pure handlers against typed message contracts first; actual socket
accept/reader loops can be wired in after TV2's SocketTransport is available.
"""

from lancollab.common.contracts import Envelope, OutboundEvent, SessionIdentity


class CollabServer:
    """Server-side handler boundary. No networking is implemented here."""

    def handle_message(
        self, message: Envelope, session: SessionIdentity
    ) -> list[OutboundEvent]:
        """Validate session and return routing actions to be queued."""
        raise NotImplementedError("TV1: implement auth/presence/chat dispatch")
