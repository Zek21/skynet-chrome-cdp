"""RFC 6455 WebSocket transport, standard library only.

The common Python CDP clients depend on `websocket-client` or `websockets`. That
is a reasonable choice and this project deliberately does not make it, for one
operational reason: this connector's job is to attach to a browser that is already
running on a machine somebody else administers. On those machines `pip install` is
frequently not available -- a locked-down corporate image, a CI container built
from a lockfile, a remote host reachable only through a command channel.

A connector that cannot be transmitted as a file and run is a connector that does
not work where browser automation is actually needed. So the transport is ~200
lines of stdlib.

Scope: a client, not a server. Text frames, binary frames, client-side masking,
continuation frames, ping/pong, and close. That is the complete subset CDP uses.
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

__all__ = ["WebSocketError", "WebSocket"]

# Frame opcodes (RFC 6455 §5.2)
_OP_CONTINUATION = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

_MAX_HEADER_BYTES = 64 * 1024


class WebSocketError(Exception):
    """Raised for handshake refusal, protocol violation, or a closed peer."""


class WebSocket:
    """A minimal, correct WebSocket client.

    Not thread-safe by design: CDP is a request/response conversation, and a
    shared socket with interleaved readers is the classic source of "the response
    to call A was delivered to call B" bugs. One session per thread.
    """

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""
        self._closed = False

    # -- lifecycle ---------------------------------------------------------
    def connect(self) -> "WebSocket":
        parsed = urlparse(self.url)
        if parsed.scheme not in ("ws", "wss"):
            raise WebSocketError(f"expected ws:// or wss://, got {self.url!r}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        sock = socket.create_connection((host, port), timeout=self.timeout)
        # CDP is a chat of small frames. With Nagle enabled the kernel coalesces
        # them and adds tens of milliseconds per call -- enough that latency
        # measurements would describe Nagle rather than the browser.
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        sock.settimeout(self.timeout)
        self._sock = sock

        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        sock.sendall(handshake.encode())

        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("peer closed during handshake")
            head += chunk
            if len(head) > _MAX_HEADER_BYTES:
                raise WebSocketError("handshake response header too large")
        header_blob, _, remainder = head.partition(b"\r\n\r\n")
        status_line = header_blob.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status_line:
            raise WebSocketError(f"upgrade refused: {status_line}")
        self._buf = remainder
        self._closed = False
        return self

    def __enter__(self):
        return self.connect() if self._sock is None else self

    def __exit__(self, *_exc):
        self.close()

    # -- io ----------------------------------------------------------------
    def _read_exactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            if self._sock is None:
                raise WebSocketError("socket is closed")
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketError("peer closed the connection")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._sock is None:
            raise WebSocketError("socket is closed")
        header = bytearray()
        header.append(0x80 | opcode)  # FIN set: this client never fragments
        length = len(payload)
        # RFC 6455 §5.3: every client-to-server frame MUST be masked. Chrome
        # drops unmasked frames without an error, which presents as a hang.
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + masked)

    def send(self, text: str) -> None:
        self._send_frame(_OP_TEXT, text.encode("utf-8"))

    def recv(self) -> str:
        """Return one complete application message, reassembling fragments.

        Chrome fragments large payloads. A screenshot of a full page arrives as a
        continuation sequence; a reader that returns after the first frame yields
        a truncated base64 string that still decodes, producing a corrupt PNG
        rather than an error. That is why this loops on FIN.
        """
        chunks: list[bytes] = []
        while True:
            b0, b1 = self._read_exactly(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exactly(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exactly(8))[0]
            mask = self._read_exactly(4) if masked else None
            payload = self._read_exactly(length) if length else b""
            if mask:
                payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))

            if opcode == _OP_CLOSE:
                self._closed = True
                raise WebSocketError("peer sent close")
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode not in (_OP_TEXT, _OP_BINARY, _OP_CONTINUATION):
                raise WebSocketError(f"unexpected opcode 0x{opcode:x}")

            chunks.append(payload)
            if fin:
                return b"".join(chunks).decode("utf-8", errors="replace")

    def close(self) -> None:
        if self._sock is not None:
            try:
                if not self._closed:
                    self._send_frame(_OP_CLOSE, b"")
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._closed = True

    @property
    def connected(self) -> bool:
        return self._sock is not None and not self._closed
