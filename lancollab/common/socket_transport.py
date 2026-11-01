"""TV2 TODO: raw TCP/TLS transport and length-prefixed JSON codec.

Do not use this placeholder in production or as grading evidence.
"""

from .contracts import Envelope


import json
import struct
from .contracts import Envelope

MIN_PAYLOAD = 1
MAX_PAYLOAD = 262144

class JsonFrameCodec:
    def encode(self, message: Envelope) -> bytes:
        payload_dict = {
            "version": message.version,
            "type": message.type,
            "request_id": message.request_id,
            "payload": message.payload,
        }
        json_bytes = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
        
        if not (MIN_PAYLOAD <= len(json_bytes) <= MAX_PAYLOAD):
            raise ValueError("Payload size out of bounds")
            
        header = struct.pack(">I", len(json_bytes))
        return header + json_bytes
    
    def decode(self, frame: bytes) -> Envelope:
        # 1. Check size limits
        if not (MIN_PAYLOAD <= len(frame) <= MAX_PAYLOAD):
            raise ValueError("Frame size out of bounds")

        # 2. Parse JSON
        try:
            data = json.loads(frame.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Invalid UTF-8 or JSON payload")

        # 3. Validate required fields and types
        if not isinstance(data, dict):
            raise ValueError("Top-level envelope must be a JSON object")

        # Validate request_id string constraints (printable ASCII, 1-64 chars)
        req_id = data.get("request_id")
        if not isinstance(req_id, str) or not (1 <= len(req_id) <= 64):
            raise ValueError("request_id must be a string of 1-64 characters")
        if not all(0x21 <= ord(c) <= 0x7E for c in req_id):
            raise ValueError("request_id contains invalid non-printable characters")

        # Validate version and type
        if data.get("version") != 1 or isinstance(data.get("version"), bool):
            raise ValueError("Invalid protocol version")
        if not isinstance(data.get("type"), str):
            raise ValueError("Envelope 'type' must be a string")
        if not isinstance(data.get("payload"), dict):
            raise ValueError("Envelope 'payload' must be a JSON object")

        return Envelope(
            version=data["version"],
            type=data["type"],
            request_id=data["request_id"],
            payload=data["payload"],
        )


import socket
import ssl

class SocketTransport:
    """Implements MessageTransport over a socket connection using JsonFrameCodec[cite: 1]."""

    def __init__(self, tls_socket: socket.socket, codec: JsonFrameCodec | None = None) -> None:
        self._socket = tls_socket
        self._codec = codec if codec is not None else JsonFrameCodec()

    def _recv_exact(self, n_bytes: int) -> bytes:
        """Loops socket.recv() until exactly n_bytes are collected to handle TCP fragmentation[cite: 1, 2]."""
        buffer = bytearray()
        while len(buffer) < n_bytes:
            chunk = self._socket.recv(n_bytes - len(buffer))
            if not chunk:
                raise ConnectionError("Connection closed by peer before full frame received.")
            buffer.extend(chunk)
        return bytes(buffer)

    def send(self, message: Envelope) -> None:
        """Serializes and pushes complete length-prefixed bytes over the socket[cite: 1, 2]."""
        framed_bytes = self._codec.encode(message)
        self._socket.sendall(framed_bytes)

    def receive(self, *, timeout: float | None = None) -> Envelope:
        """Receives an exact framed message, enforcing timeout and frame length limits[cite: 1, 2]."""
        original_timeout = self._socket.gettimeout()
        if timeout is not None:
            self._socket.settimeout(timeout)

        try:
            # 1. Read 4-byte header
            header_bytes = self._recv_exact(4)
            payload_len = struct.unpack(">I", header_bytes)[0]

            # 2. Read exact payload bytes and decode
            payload_bytes = self._recv_exact(payload_len)
            return self._codec.decode(payload_bytes)
        finally:
            if timeout is not None:
                self._socket.settimeout(original_timeout)

    def close(self) -> None:
        """Safely shuts down and closes the socket endpoint[cite: 1]."""
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        finally:
            self._socket.close()

# --- TLS Helpers for Issue #10 ---

HANDSHAKE_TIMEOUT = 5.0  

def wrap_server_socket(raw_sock: socket.socket, certfile: str, keyfile: str) -> ssl.SSLSocket:
    """Wraps server socket with a 5s deadline[cite: 1, 2]."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2  # Fixed: added 'v'
    context.load_cert_chain(certfile=certfile, keyfile=keyfile)
    
    raw_sock.settimeout(HANDSHAKE_TIMEOUT)
    try:
        return context.wrap_socket(raw_sock, server_side=True)
    except (ssl.SSLError, TimeoutError, OSError) as err:
        raw_sock.close()
        raise ConnectionError(f"TLS handshake failed: {err}") from err

def wrap_client_socket(raw_sock: socket.socket, cafile: str, server_hostname: str = "lan-collab.local") -> ssl.SSLSocket:
    """Wraps client socket, enforcing strict CA validation and hostname checks[cite: 1, 2]."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2  # Fixed: added 'v'
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    context.load_verify_locations(cafile=cafile)
    
    raw_sock.settimeout(HANDSHAKE_TIMEOUT)
    try:
        return context.wrap_socket(raw_sock, server_side=False, server_hostname=server_hostname)
    except (ssl.SSLError, TimeoutError, OSError) as err:
        raw_sock.close()
        raise ConnectionError(f"TLS verification failed: {err}") from err