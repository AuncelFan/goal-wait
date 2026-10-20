"""Dependency-free local Codex app-server WebSocket transport (Unix or loopback).

Only JSON text messages are needed. Client frames are masked; fragmentation and
ping/pong are supported. This is deliberately not a general WebSocket library.
"""

import base64
import hashlib
import ipaddress
import json
import os
import queue
import socket
import struct
import threading
from urllib.parse import urlparse


class RpcError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, endpoint):
        self.lock = threading.Lock()
        self.buffer = bytearray()
        if endpoint.startswith("unix://"):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect(endpoint[7:])
            host, resource = "localhost", "/"
        else:
            url = urlparse(endpoint)
            if url.scheme != "ws" or not url.hostname or url.username or url.password:
                raise ValueError("Use unix:///path/to/socket or ws://127.0.0.1:port")
            # No remote plaintext control connections or DNS aliases.
            if not ipaddress.ip_address(url.hostname).is_loopback:
                raise ValueError("Only loopback WebSocket endpoints are supported")
            self.sock = socket.create_connection((url.hostname, url.port or 80), 10)
            host = url.netloc
            resource = url.path or "/"
            if url.query:
                resource += "?" + url.query
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {resource} HTTP/1.1\r\nHost: {host}\r\n"
                           "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        while b"\r\n\r\n" not in self.buffer:
            self.buffer.extend(self._receive())
            if len(self.buffer) > 65536:
                raise RpcError("Oversized WebSocket handshake")
        headers, rest = self.buffer.split(b"\r\n\r\n", 1)
        self.buffer = bytearray(rest)
        lines = headers.decode("latin1").split("\r\n")
        fields = {key.lower(): value.strip() for key, value in
                  (line.split(":", 1) for line in lines[1:] if ":" in line)}
        expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if lines[0].split()[1] != "101" or fields.get("sec-websocket-accept") != expected:
            self.close()
            raise RpcError("App-server rejected WebSocket upgrade")
        self.sock.settimeout(None)

    def _receive(self):
        data = self.sock.recv(65536)
        if not data:
            raise RpcError("App-server disconnected")
        return data

    def read(self, size):
        while len(self.buffer) < size:
            self.buffer.extend(self._receive())
        data = bytes(self.buffer[:size])
        del self.buffer[:size]
        return data

    def send(self, data, opcode=1):
        payload = data.encode() if isinstance(data, str) else data
        size = len(payload)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += b"\xfe" + struct.pack("!H", size)
        else:
            header += b"\xff" + struct.pack("!Q", size)
        mask = os.urandom(4)
        masked = bytes(v ^ mask[i % 4] for i, v in enumerate(payload))
        with self.lock:
            self.sock.sendall(header + mask + masked)

    def receive_text(self):
        fragments = bytearray()
        while True:
            first, second = self.read(2)
            opcode, size = first & 15, second & 127
            if first & 0x70 or second & 0x80:
                raise RpcError("Unexpected WebSocket extension or masked server frame")
            if size == 126:
                size = struct.unpack("!H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read(8))[0]
            if size + len(fragments) > 32 * 1024 * 1024:
                raise RpcError("WebSocket message exceeds 32 MiB")
            data = self.read(size)
            if opcode == 8:
                raise RpcError("App-server closed the connection")
            if opcode == 9:
                self.send(data, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1):
                raise RpcError("Expected a WebSocket text frame")
            fragments.extend(data)
            if first & 0x80:
                return fragments.decode()

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class Client:
    def __init__(self, endpoint, events=None):
        self.events = events if events is not None else queue.Queue()
        self.ws = WebSocket(endpoint)
        self.pending = {}
        self.lock = threading.Lock()
        self.sequence = 0
        self.closed = False
        threading.Thread(target=self._read, daemon=True).start()
        self.info = self.call("initialize", {
            "clientInfo": {"name": "goal_wait", "version": "0.1.0"},
            "capabilities": {"experimentalApi": True},
        })
        self.ws.send(json.dumps({"method": "initialized", "params": {}}))

    def _read(self):
        try:
            while True:
                message = json.loads(self.ws.receive_text())
                if "id" in message and "method" not in message:
                    with self.lock:
                        waiting = self.pending.get(message["id"])
                    if waiting is not None:
                        waiting.put(message)
                elif "id" in message:
                    # This observer does not own approval/elicitation requests.
                    # Decline instead of hanging the thread or granting approval.
                    self.ws.send(json.dumps({"id": message["id"], "error": {
                        "code": -32601, "message": "goal-wait is an observer; use the interactive client"}}))
                else:
                    self.events.put(message)
        except Exception as error:
            self.closed = True
            self.events.put({"method": "_disconnected", "params": {"error": str(error)}})
            with self.lock:
                for waiting in self.pending.values():
                    waiting.put({"error": {"message": str(error)}})

    def call(self, method, params, timeout=15):
        waiting = queue.Queue()
        with self.lock:
            if self.closed:
                raise RpcError("App-server connection is closed")
            self.sequence += 1
            sequence = self.sequence
            self.pending[sequence] = waiting
        try:
            self.ws.send(json.dumps({"id": sequence, "method": method, "params": params}))
            try:
                response = waiting.get(timeout=timeout)
            except queue.Empty as error:
                raise RpcError(f"RPC timeout: {method}; outcome may be uncertain") from error
            if "error" in response:
                raise RpcError(f"{method}: {response['error']}")
            return response.get("result", {})
        finally:
            with self.lock:
                self.pending.pop(sequence, None)

    def close(self):
        self.ws.close()
