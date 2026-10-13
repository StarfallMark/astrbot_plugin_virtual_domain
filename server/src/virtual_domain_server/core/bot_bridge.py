"""Authenticated duplex transport from the world service to AstrBot."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import secrets
import uuid
import queue
import socket
import struct
import threading
import time
from typing import Any


class BotBridge:
    def __init__(self, token: str, *, timeout: float = 240) -> None:
        self.token = token
        self.timeout = timeout
        self.loop: asyncio.AbstractEventLoop | None = None
        self.socket: Any = None
        self.pending: dict[str, asyncio.Future] = {}
        self.info: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return self.socket is not None

    async def accept(self, socket: Any) -> None:
        await socket.accept()
        try:
            hello = await asyncio.wait_for(socket.receive_json(), 10)
            header_token = str(socket.headers.get("x-connector-token", ""))
            if (
                hello.get("type") != "hello"
                or hello.get("protocol_version") != 1
                or not self.token
                or not secrets.compare_digest(header_token, self.token)
            ):
                await socket.close(code=1008)
                return
            if self.socket:
                await socket.close(code=1013)
                return
            self.loop = asyncio.get_running_loop()
            self.socket = socket
            self.info = {
                "plugin_version": str(hello.get("plugin_version", ""))[:64],
                "default_world_id": str(hello.get("default_world_id", ""))[:128],
                "capabilities": [str(item)[:64] for item in hello.get("capabilities", [])[:32]]
                if isinstance(hello.get("capabilities", []), list) else [],
            }
            await socket.send_json({"type": "ready", "protocol_version": 1, "server_version": "0.1.0"})
            while True:
                message = await socket.receive_json()
                if message.get("type") == "ping":
                    await socket.send_json({"type": "pong"})
                    continue
                future = self.pending.get(str(message.get("request_id", "")))
                if future and not future.done() and message.get("type") == "response":
                    response = message.get("response")
                    future.set_result(response if isinstance(response, dict) else {"error": "AstrBot 返回格式无效"})
        except Exception:
            pass
        finally:
            if self.socket is socket:
                self.socket = None
                self.info = {}
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(RuntimeError("AstrBot 连接中断"))

    async def _request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.socket:
            raise RuntimeError("AstrBot 未连接，请先启动 AstrBot 并检查连接器配置")
        request_id = str(payload.get("request_id") or uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.socket.send_json({"type": "request", **payload, "request_id": request_id})
            result = await asyncio.wait_for(future, self.timeout)
            return result
        finally:
            self.pending.pop(request_id, None)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.loop or not self.connected:
            raise RuntimeError("AstrBot 未连接，请先启动 AstrBot 并检查连接器配置")
        task = asyncio.run_coroutine_threadsafe(self._request(payload), self.loop)
        try:
            return task.result(self.timeout + 5)
        except concurrent.futures.TimeoutError as exc:
            task.cancel()
            raise RuntimeError("AstrBot 响应超时") from exc


class PollingBotBridge:
    """线程安全的 HTTP long-poll bridge used by the AstrBot connector."""

    def __init__(self, token: str, *, timeout: float = 240) -> None:
        self.token = token
        self.timeout = timeout
        self.requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self.pending: dict[str, queue.Queue[dict[str, Any]]] = {}
        self.lock = threading.RLock()
        self.last_poll = 0.0

    @property
    def connected(self) -> bool:
        return time.monotonic() - self.last_poll < 45

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.connected:
            raise RuntimeError("AstrBot 未连接，请启动 AstrBot 并检查插件连接凭证")
        request_id = str(payload.get("request_id") or uuid.uuid4())
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.lock:
            self.pending[request_id] = response_queue
        self.requests.put({**payload, "request_id": request_id})
        try:
            try:
                return response_queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise RuntimeError("AstrBot 响应超时") from exc
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def poll(self, timeout: float = 20) -> dict[str, Any] | None:
        self.last_poll = time.monotonic()
        try:
            deadline = time.monotonic() + timeout
            while True:
                value = self.requests.get(timeout=max(0.01, deadline - time.monotonic()))
                with self.lock:
                    if value["request_id"] in self.pending:
                        return value
        except queue.Empty:
            return None

    def respond(self, request_id: str, value: dict[str, Any]) -> bool:
        with self.lock:
            target = self.pending.get(request_id)
        if target is None:
            return False
        try:
            target.put_nowait(value)
            return True
        except queue.Full:
            return False


def receive_websocket_frame(connection: socket.socket, *, expect_masked: bool) -> tuple[int, bytes]:
    first, second = _receive_exact(connection, 2)
    fin = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    if first & 0x70 or not fin or masked != expect_masked:
        raise ValueError("Invalid WebSocket frame")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _receive_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _receive_exact(connection, 8))[0]
    if length > 2 * 1024 * 1024 or (opcode >= 8 and length > 125):
        raise ValueError("WebSocket frame exceeds the protocol limit")
    mask = _receive_exact(connection, 4) if masked else b""
    payload = _receive_exact(connection, length)
    if masked:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


def _receive_exact(connection: socket.socket, length: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < length:
        value = connection.recv(length - len(chunks))
        if not value:
            raise ConnectionError("WebSocket connection closed")
        chunks.extend(value)
    return bytes(chunks)


def send_websocket_frame(connection: socket.socket, opcode: int, payload: bytes = b"") -> None:
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length < 126:
        header.append(length)
    elif length <= 0xFFFF:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    connection.sendall(header + payload)


class WebSocketBotBridge(PollingBotBridge):
    """Persistent authenticated bridge with long-poll compatibility."""

    def __init__(self, token: str, *, timeout: float = 240) -> None:
        super().__init__(token, timeout=timeout)
        self.socket: socket.socket | None = None
        self.socket_send_lock = threading.Lock()
        self.info: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        with self.lock:
            if self.socket is not None:
                return True
        return super().connected

    def attach(self, connection: socket.socket) -> bool:
        with self.lock:
            if self.socket is not None:
                return False
            self.socket = connection
            return True

    def set_client_info(self, connection: socket.socket, info: dict[str, Any]) -> bool:
        with self.lock:
            if self.socket is not connection:
                return False
            self.info = info
            return True

    def send_json(self, connection: socket.socket, value: dict[str, Any]) -> None:
        raw = json_dumps(value)
        with self.socket_send_lock:
            with self.lock:
                if self.socket is not connection:
                    raise ConnectionError("AstrBot WebSocket 已断开")
            send_websocket_frame(connection, 1, raw)

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            connection = self.socket
        if connection is None:
            return super().request(payload)
        request_id = str(payload.get("request_id") or uuid.uuid4())
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self.lock:
            fallback = self.socket is not connection
            if not fallback:
                self.pending[request_id] = response_queue
        if fallback:
            return super().request(payload)
        try:
            self.send_json(connection, {"type": "request", **payload, "request_id": request_id})
            try:
                response = response_queue.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise RuntimeError("AstrBot 响应超时") from exc
            return response
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def detach(self, connection: socket.socket) -> None:
        with self.lock:
            if self.socket is not connection:
                return
            self.socket = None
            self.info = {}
            pending = list(self.pending.values())
        for response_queue in pending:
            try:
                response_queue.put_nowait({"error": "AstrBot WebSocket 已断开"})
            except queue.Full:
                pass

    def respond_message(self, value: dict[str, Any]) -> bool:
        if value.get("type") != "response":
            return False
        response = value.get("response")
        if not isinstance(response, dict):
            response = {"error": "AstrBot 返回格式无效"}
        return self.respond(str(value.get("request_id", "")), response)


def json_dumps(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
