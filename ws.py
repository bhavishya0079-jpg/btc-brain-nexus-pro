"""Minimal stdlib RFC 6455 WebSocket client with auto-reconnect, plus a ticker stream hub."""
import base64
import hashlib
import json
import logging
import os
import socket
import ssl
import struct
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger("nexus.ws")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def encode_frame(opcode, payload=b"", mask=True):
    if isinstance(payload, str):
        payload = payload.encode()
    b0 = 0x80 | opcode
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        hdr = struct.pack("!BB", b0, mbit | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", b0, mbit | 126, n)
    else:
        hdr = struct.pack("!BBQ", b0, mbit | 127, n)
    if mask:
        key = os.urandom(4)
        masked = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return hdr + key + masked
    return hdr + payload


def read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return buf


def read_frame(sock):
    """Returns (fin, opcode, payload)."""
    b0, b1 = struct.unpack("!BB", read_exact(sock, 2))
    fin, opcode = bool(b0 & 0x80), b0 & 0x0F
    masked, n = bool(b1 & 0x80), b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", read_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack("!Q", read_exact(sock, 8))[0]
    key = read_exact(sock, 4) if masked else None
    data = read_exact(sock, n) if n else b""
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return fin, opcode, data


class WSClient(threading.Thread):
    def __init__(self, url, on_message, on_open_send=None, ping_interval=20, max_backoff=60, connect_timeout=10):
        super().__init__(daemon=True, name="ws-" + urlparse(url).netloc)
        self.url, self.on_message, self.on_open_send = url, on_message, on_open_send
        self.ping_interval, self.max_backoff, self.connect_timeout = ping_interval, max_backoff, connect_timeout
        self._halt = threading.Event()
        self.sock = None
        self.state = "init"
        self.reconnects = 0
        self.last_error = None
        self.last_message_at = None

    def stop(self):
        self._halt.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass

    def _connect(self):
        u = urlparse(self.url)
        tls = u.scheme == "wss"
        port = u.port or (443 if tls else 80)
        raw = socket.create_connection((u.hostname, port), timeout=self.connect_timeout)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if tls else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {u.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: nexus\r\n\r\n")
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(1024)
            if not chunk:
                raise ConnectionError("handshake closed")
            resp += chunk
            if len(resp) > 16384:
                raise ConnectionError("handshake too large")
        head = resp.split(b"\r\n\r\n")[0].decode(errors="replace")
        if " 101 " not in head.split("\r\n")[0]:
            raise ConnectionError("handshake rejected: " + head.split("\r\n")[0])
        want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        if want.lower() not in head.lower():
            raise ConnectionError("bad Sec-WebSocket-Accept")
        return sock

    def send_json(self, obj):
        self.sock.sendall(encode_frame(0x1, json.dumps(obj)))

    def run(self):
        backoff = 1
        while not self._halt.is_set():
            try:
                self.state = "connecting"
                self.sock = self._connect()
                self.sock.settimeout(self.ping_interval)
                self.state = "open"
                backoff = 1
                if self.on_open_send:
                    self.send_json(self.on_open_send)
                self._loop()
            except Exception as e:
                self.last_error = str(e)[:200]
                self.state = "reconnecting"
            finally:
                try:
                    if self.sock:
                        self.sock.close()
                except Exception:
                    pass
            if self._halt.is_set():
                break
            self.reconnects += 1
            self._halt.wait(backoff)
            backoff = min(backoff * 2, self.max_backoff)
        self.state = "stopped"

    def _loop(self):
        text = b""
        while not self._halt.is_set():
            try:
                fin, op, data = read_frame(self.sock)
            except socket.timeout:
                self.sock.sendall(encode_frame(0x9, b"ping"))      # keep-alive
                continue
            if op == 0x9:
                self.sock.sendall(encode_frame(0xA, data))
            elif op == 0x8:
                raise ConnectionError("server closed")
            elif op in (0x1, 0x0, 0x2):
                text += data
                if fin:
                    msg, text = text, b""
                    self.last_message_at = time.time()
                    try:
                        self.on_message(json.loads(msg))
                    except (ValueError, KeyError):
                        pass


class StreamHub:
    """Keeps the latest tick per (provider, symbol) from exchange WebSockets. Falls back to REST in the caller."""

    def __init__(self, manager):
        self.manager = manager
        self.latest = {}
        self.clients = {}
        self.lock = threading.Lock()

    def ensure(self, provider, symbols):
        p = self.manager.get(provider)
        if not hasattr(p, "ws_url"):
            return False
        key = (provider, tuple(sorted(symbols)))
        with self.lock:
            if key in self.clients:
                return True
            url, sub = p.ws_url(symbols)

            def on_msg(m, p=p, provider=provider):
                t = p.parse_ws(m)
                if t:
                    self.latest[(provider, t["symbol"])] = (time.time(), t)

            c = WSClient(url, on_msg, sub)
            self.clients[key] = c
            c.start()
        return True

    def get(self, provider, symbol, max_age=15):
        hit = self.latest.get((provider, symbol))
        if hit and time.time() - hit[0] <= max_age:
            return hit[1]
        return None

    def status(self):
        return [{"provider": k[0], "symbols": list(k[1]), "state": c.state, "reconnects": c.reconnects,
                 "last_error": c.last_error, "last_message_at": c.last_message_at}
                for k, c in self.clients.items()]

    def shutdown(self):
        for c in self.clients.values():
            c.stop()
