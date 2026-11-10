"""TV1 TODO: server session manager, authentication and chat routing.

The TCP/TLS connection milestone lives in connections.py. This handler boundary
is not wired to it yet; credentials are never accepted by the connection demo.
"""

from lancollab.common.contracts import Envelope, OutboundEvent, SessionIdentity


class CollabServer:
    """Server-side handler boundary. No networking is implemented here."""

    def handle_message(self, message: Envelope, session: SessionIdentity) -> list[OutboundEvent]:
        """Validate session and return routing actions to be queued."""
        raise NotImplementedError("TV1: implement auth/presence/chat dispatch")
