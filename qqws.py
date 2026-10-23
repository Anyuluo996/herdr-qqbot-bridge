#!/usr/bin/env python3
"""Minimal RFC 6455 WebSocket client, stdlib only — as much as QQ's gateway needs.

Covers the wss/ws handshake, masked client frames, server frames of any length,
WS-level ping/pong, close, and fragmented data messages. The partial-frame
buffer survives a socket timeout, so a quiet wire never loses bytes; the one
gap is a timeout in the middle of a fragmented message, where the fragments
assembled so far are dropped — the gateway doesn't fragment in practice.
"""

import base64
import hashlib
import os
import re
import socket
import ssl
import struct

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_URL = re.compile(r"^(wss?)://([^/:?]+)(?::(\d+))?(/[^?]*)?(\?.*)?$")

CONT, TEXT, BINARY, CLOSE, PING, PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class Closed(Exception):
    """Peer closed the connection (or sent a close frame)."""


class HandshakeError(Exception):
    pass


class WS:
    def __init__(self):
        self.sock = None
        self.buf = b""

    def connect(self, url: str, timeout: float = 15.0) -> None:
        m = _URL.match(url)
        if not m:
            raise HandshakeError(f"not a ws url: {url!r}")
        scheme, host, port, path, query = m.groups()
        port = int(port or (443 if scheme == "wss" else 80))
        raw = socket.create_connection((host, port), timeout=timeout)
        if scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path or '/'}{query or ''} HTTP/1.1\r\n"
               f"Host: {host}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               "User-Agent: herdr-qqbot-bridge\r\n\r\n")
        self.sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n", timeout)
        lines = head.decode("latin1").split("\r\n")
        if " 101" not in (lines[0] if lines else ""):
            raise HandshakeError(f"upgrade refused: {(lines[0] if lines else 'empty')[:120]}")
        accept = ""
        for ln in lines[1:]:
            if ln.lower().startswith("sec-websocket-accept:"):
                accept = ln.split(":", 1)[1].strip()
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
        if accept != want:
            raise HandshakeError("bad Sec-WebSocket-Accept")
        # keep whatever bytes rode in past the header terminator — on loopback
        # the first frames often coalesce with the 101 response, and clearing
        # buf here silently eats them

    def _read_until(self, marker: bytes, timeout: float) -> bytes:
        self.sock.settimeout(timeout)
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise Closed("eof during handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head

    # -- sending ------------------------------------------------------------

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise Closed("not connected")
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        if n < 126:
            head = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            head = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            head = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(TEXT, text.encode())

    def send_pong(self, payload: bytes = b"") -> None:
        self._send_frame(PONG, payload)

    def send_close(self, code: int = 1000) -> None:
        try:
            self._send_frame(CLOSE, struct.pack("!H", code))
        except OSError:
            pass

    # -- receiving ----------------------------------------------------------

    def _recv_exact(self, n: int, timeout: float) -> bytes:
        """n bytes, surviving short reads; a timeout keeps partial bytes in buf."""
        self.sock.settimeout(timeout)
        while len(self.buf) < n:
            chunk = self.sock.recv(min(65536, n - len(self.buf)))
            if not chunk:
                raise Closed("eof")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, timeout: float) -> tuple[int, bool, bytes]:
        b1, b2 = self._recv_exact(2, timeout)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        n = b2 & 0x7F
        if b2 & 0x80:  # servers must not mask; refuse rather than mis-parse
            raise Closed("server sent a masked frame")
        if n == 126:
            (n,) = struct.unpack("!H", self._recv_exact(2, timeout))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._recv_exact(8, timeout))
        if not n:
            return opcode, fin, b""
        return opcode, fin, self._recv_exact(n, timeout)

    def recv_message(self, timeout: float) -> tuple[int, bytes]:
        """Next data message; answers WS pings, drops pongs, raises Closed on close.
        A quiet wire raises socket.timeout with parse state intact in self.buf."""
        frags: list[bytes] = []
        frag_op = TEXT
        while True:
            opcode, fin, payload = self._frame(timeout)
            if opcode == PING:
                self.send_pong(payload)
                continue
            if opcode == PONG:
                continue
            if opcode == CLOSE:
                self.send_close()
                raise Closed("close frame")
            if opcode in (TEXT, BINARY):
                if fin:
                    return opcode, payload
                frags, frag_op = [payload], opcode
            elif opcode == CONT and frags:
                frags.append(payload)
                if fin:
                    return frag_op, b"".join(frags)

    def close(self) -> None:
        self.send_close()
        try:
            if self.sock is not None:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
