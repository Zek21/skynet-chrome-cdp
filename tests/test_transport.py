"""RFC 6455 conformance for the stdlib transport.

Tested against a real socket server that speaks the protocol back, not a mock.
A mock would encode this file's assumptions about WebSocket framing, and those
assumptions are exactly what needs checking.
"""
from __future__ import annotations

import base64
import hashlib
import socket
import struct
import threading
import unittest

from skynet_chrome_cdp.transport import WebSocket, WebSocketError

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class EchoServer(threading.Thread):
    """One-connection RFC 6455 server used as the test oracle."""

    def __init__(self, fragment=False, send_ping=False, refuse=False):
        super().__init__(daemon=True)
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self.fragment = fragment
        self.send_ping = send_ping
        self.refuse = refuse
        self.received: list[str] = []
        self.frames_were_masked: list[bool] = []

    def run(self):
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            if self.refuse:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            key = ""
            for line in head.decode("latin-1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key = line.split(":", 1)[1].strip()
            accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
            conn.sendall(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept.encode() + b"\r\n\r\n"
            )

            buf = b""

            def read(n):
                nonlocal buf
                while len(buf) < n:
                    more = conn.recv(65536)
                    if not more:
                        raise OSError("closed")
                    buf += more
                out, buf = buf[:n], buf[n:]
                return out

            while True:
                b0, b1 = read(2)
                opcode = b0 & 0x0F
                masked = bool(b1 & 0x80)
                length = b1 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", read(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", read(8))[0]
                mask = read(4) if masked else None
                payload = read(length) if length else b""
                if mask:
                    payload = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
                if opcode == 0x8:
                    return
                if opcode in (0x9, 0xA):
                    continue
                self.frames_were_masked.append(masked)
                text = payload.decode("utf-8", errors="replace")
                self.received.append(text)

                data = text.encode("utf-8")
                if self.send_ping:
                    conn.sendall(b"\x89\x00")
                if self.fragment and len(data) > 4:
                    half = len(data) // 2
                    conn.sendall(bytes([0x01, len(data[:half])]) + data[:half])
                    conn.sendall(bytes([0x80, len(data[half:])]) + data[half:])
                else:
                    header = bytearray([0x81])
                    n = len(data)
                    if n < 126:
                        header.append(n)
                    elif n < (1 << 16):
                        header.append(126)
                        header += struct.pack(">H", n)
                    else:
                        header.append(127)
                        header += struct.pack(">Q", n)
                    conn.sendall(bytes(header) + data)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def stop(self):
        try:
            self._listener.close()
        except OSError:
            pass


class TransportTest(unittest.TestCase):
    def _connect(self, **kwargs) -> tuple[WebSocket, EchoServer]:
        server = EchoServer(**kwargs)
        server.start()
        self.addCleanup(server.stop)
        ws = WebSocket(f"ws://127.0.0.1:{server.port}/devtools/page/ABC", timeout=5)
        ws.connect()
        self.addCleanup(ws.close)
        return ws, server

    def test_handshake_and_echo(self):
        ws, _ = self._connect()
        ws.send("hello")
        self.assertEqual(ws.recv(), "hello")

    def test_client_frames_are_always_masked(self):
        """RFC 6455 §5.3. Chrome silently drops unmasked client frames, so the
        symptom of getting this wrong is a hang, not an error."""
        ws, server = self._connect()
        ws.send("payload")
        ws.recv()
        self.assertEqual(server.frames_were_masked, [True])

    def test_16_bit_extended_length(self):
        ws, _ = self._connect()
        payload = "x" * 5000
        ws.send(payload)
        self.assertEqual(ws.recv(), payload)

    def test_64_bit_extended_length(self):
        ws, _ = self._connect()
        payload = "y" * 70000
        ws.send(payload)
        self.assertEqual(ws.recv(), payload)

    def test_fragmented_reply_is_reassembled(self):
        """Screenshots arrive fragmented. Returning after the first frame yields
        truncated base64 that still decodes -- a corrupt PNG, not an exception."""
        ws, _ = self._connect(fragment=True)
        ws.send("abcdefghij")
        self.assertEqual(ws.recv(), "abcdefghij")

    def test_ping_is_ponged_without_corrupting_the_stream(self):
        ws, _ = self._connect(send_ping=True)
        ws.send("after-ping")
        self.assertEqual(ws.recv(), "after-ping")

    def test_utf8_multibyte_survives_the_round_trip(self):
        ws, _ = self._connect()
        payload = "café — 東京 — 🚀"
        ws.send(payload)
        self.assertEqual(ws.recv(), payload)

    def test_rejected_upgrade_raises_with_the_status(self):
        server = EchoServer(refuse=True)
        server.start()
        self.addCleanup(server.stop)
        ws = WebSocket(f"ws://127.0.0.1:{server.port}/x", timeout=5)
        with self.assertRaises(WebSocketError) as ctx:
            ws.connect()
        self.assertIn("403", str(ctx.exception))

    def test_non_websocket_scheme_is_refused(self):
        with self.assertRaises(WebSocketError):
            WebSocket("http://127.0.0.1:1/x").connect()

    def test_connected_property_tracks_state(self):
        ws, _ = self._connect()
        self.assertTrue(ws.connected)
        ws.close()
        self.assertFalse(ws.connected)

    def test_close_is_idempotent(self):
        ws, _ = self._connect()
        ws.close()
        ws.close()  # must not raise
        self.assertFalse(ws.connected)

    def test_send_after_close_raises_rather_than_silently_dropping(self):
        ws, _ = self._connect()
        ws.close()
        with self.assertRaises(WebSocketError):
            ws.send("late")


if __name__ == "__main__":
    unittest.main()
