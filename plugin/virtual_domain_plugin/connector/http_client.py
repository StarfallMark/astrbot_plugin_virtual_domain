"""AstrBot 连接器使用的轻量 HTTP 客户端。"""

from __future__ import annotations

import json
import base64
import hashlib
import os
import ssl
import socket
import struct
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode, urlsplit


class ConnectorError(RuntimeError):
    pass


class BridgeWebSocket:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.send_lock = threading.Lock()
        self.closed = False

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.closed:
            raise ConnectorError("虚拟域 WebSocket 已断开")
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked_payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        with self.send_lock:
            self.connection.sendall(header + mask + masked_payload)

    def send_json(self, value: dict) -> None:
        try:
            self._send_frame(1, json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except OSError as exc:
            raise ConnectorError(f"发送 AstrBot 桥接消息失败：{exc}") from exc

    def send_ping(self) -> None:
        self._send_frame(9, b"vd")

    def _receive_exact(self, length: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < length:
            value = self.connection.recv(length - len(chunks))
            if not value:
                raise ConnectorError("虚拟域 WebSocket 已断开")
            chunks.extend(value)
        return bytes(chunks)

    def receive_json(self) -> dict:
        while True:
            try:
                first, second = self._receive_exact(2)
                if first & 0x70 or not first & 0x80 or second & 0x80:
                    raise ConnectorError("虚拟域 WebSocket 帧格式无效")
                opcode = first & 0x0F
                length = second & 0x7F
                if length == 126:
                    length = struct.unpack("!H", self._receive_exact(2))[0]
                elif length == 127:
                    length = struct.unpack("!Q", self._receive_exact(8))[0]
                if length > 2 * 1024 * 1024 or (opcode >= 8 and length > 125):
                    raise ConnectorError("虚拟域 WebSocket 消息超过长度限制")
                payload = self._receive_exact(length)
                if opcode == 8:
                    raise ConnectorError("虚拟域服务端已关闭 WebSocket")
                if opcode == 9:
                    self._send_frame(10, payload)
                    continue
                if opcode == 10:
                    continue
                if opcode != 1:
                    raise ConnectorError("虚拟域 WebSocket 只接受 JSON 文本消息")
                result = json.loads(payload.decode("utf-8"))
                if not isinstance(result, dict):
                    raise ConnectorError("虚拟域 WebSocket 返回了无效消息")
                return result
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ConnectorError(f"接收 AstrBot 桥接消息失败：{exc}") from exc

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            mask = os.urandom(4)
            payload = struct.pack("!H", 1000)
            with self.send_lock:
                self.connection.sendall(bytes([0x88, 0x82]) + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))
        except OSError:
            pass
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.connection.close()


class VirtualDomainClient:
    def __init__(
        self,
        endpoint: str,
        token: str = "",
        *,
        tls_ca_file: str = "",
        tls_client_cert_file: str = "",
        tls_client_key_file: str = "",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.tls_ca_file = tls_ca_file.strip()
        self.tls_client_cert_file = tls_client_cert_file.strip()
        self.tls_client_key_file = tls_client_key_file.strip()

    def _tls_context(self):
        cert = self.tls_client_cert_file
        key = self.tls_client_key_file
        if bool(cert) != bool(key):
            raise ConnectorError("双向 TLS 客户端证书与私钥必须同时配置")
        if not self.endpoint.lower().startswith("https://"):
            if self.tls_ca_file or cert or key:
                raise ConnectorError("配置了 TLS 证书时，服务端地址必须以 https:// 开头")
            return None
        try:
            context = ssl.create_default_context(cafile=self.tls_ca_file or None)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            if cert:
                context.load_cert_chain(certfile=cert, keyfile=key)
            return context
        except (OSError, ssl.SSLError) as exc:
            raise ConnectorError(f"TLS 证书配置无效：{exc}") from exc

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.endpoint:
            raise ConnectorError("尚未配置虚拟域服务端地址")
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(f"{self.endpoint}{path}", data=data, headers=headers, method=method)
        try:
            context = self._tls_context()
            opener_options = {"context": context} if context is not None else {}
            with urlopen(request, timeout=600 if path.endswith(("/actions", "/chat")) else 30, **opener_options) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except Exception:
                result = {"message": str(exc)}
            finally:
                exc.close()
            detail = result.get("detail", result) if isinstance(result, dict) else result
            message = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            raise ConnectorError(message) from exc
        except URLError as exc:
            raise ConnectorError(f"连接虚拟域失败：{exc.reason}") from exc
        if not isinstance(result, dict):
            raise ConnectorError("虚拟域返回了无效数据")
        return result

    def current_world(self) -> dict:
        worlds = self.worlds().get("worlds", [])
        world = next((item for item in worlds if item.get("current")), None)
        if not world:
            raise ConnectorError("服务端尚未选择当前世界，请管理员在客户端选择")
        return world

    def health(self) -> dict:
        return self.request("GET", "/api/v1/health")

    def worlds(self) -> dict:
        return self.request_with_headers("GET", "/api/v1/bridge/worlds", headers={"X-Connector-Token": self.token})

    def bind_qq(self, qq_id: str, code: str, *, session_origin: str = "") -> dict:
        return self.request_with_headers(
            "POST",
            "/api/v1/bridge/qq/bind",
            {"qq_id": qq_id, "code": code, "session_origin": session_origin},
            headers={"X-Connector-Token": self.token},
        )

    def admin_switch_world(
        self,
        world_id: str,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
        confirm_online_disconnect: bool = False,
    ) -> dict:
        return self.request_with_headers(
            "POST",
            f"/api/v1/bridge/admin/worlds/{world_id}/current",
            {
                "sender_qq_id": sender_qq_id,
                "configured_admin_qq": configured_admin_qq,
                "confirm_online_disconnect": confirm_online_disconnect,
            },
            headers={"X-Connector-Token": self.token},
        )

    def admin_set_world_paused(
        self, world_id: str, *, sender_qq_id: str, configured_admin_qq: str, paused: bool
    ) -> dict:
        return self.request_with_headers(
            "POST",
            f"/api/v1/bridge/admin/worlds/{world_id}/state",
            {
                "sender_qq_id": sender_qq_id,
                "configured_admin_qq": configured_admin_qq,
                "paused": paused,
            },
            headers={"X-Connector-Token": self.token},
        )

    def admin_delete_world(
        self,
        world_id: str,
        confirmed_world_name: str,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
    ) -> dict:
        return self.request_with_headers(
            "POST",
            f"/api/v1/bridge/admin/worlds/{world_id}/delete",
            {
                "sender_qq_id": sender_qq_id,
                "configured_admin_qq": configured_admin_qq,
                "confirmed_world_name": confirmed_world_name,
            },
            headers={"X-Connector-Token": self.token},
        )

    def admin_create_invitation(
        self,
        world_id: str,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
        hours: int = 24,
    ) -> dict:
        return self.request_with_headers(
            "POST",
            f"/api/v1/bridge/admin/worlds/{world_id}/invitations",
            {
                "sender_qq_id": sender_qq_id,
                "configured_admin_qq": configured_admin_qq,
                "hours": hours,
            },
            headers={"X-Connector-Token": self.token},
        )

    def admin_revoke_invitation(
        self,
        world_id: str,
        code: str,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
    ) -> dict:
        return self.request_with_headers(
            "DELETE",
            f"/api/v1/bridge/admin/worlds/{world_id}/invitations",
            {
                "sender_qq_id": sender_qq_id,
                "configured_admin_qq": configured_admin_qq,
                "code": code,
            },
            headers={"X-Connector-Token": self.token},
        )

    def admin_create_backup(self, *, sender_qq_id: str, configured_admin_qq: str) -> dict:
        return self.request_with_headers(
            "POST",
            "/api/v1/bridge/admin/backups",
            {"sender_qq_id": sender_qq_id, "configured_admin_qq": configured_admin_qq},
            headers={"X-Connector-Token": self.token},
        )

    def admin_list_backups(self, *, sender_qq_id: str, configured_admin_qq: str) -> dict:
        query = urlencode({"sender_qq_id": sender_qq_id, "configured_admin_qq": configured_admin_qq})
        return self.request_with_headers(
            "GET",
            f"/api/v1/bridge/admin/backups?{query}",
            headers={"X-Connector-Token": self.token},
        )

    def admin_quota_snapshot(
        self, world_id: str, *, sender_qq_id: str, configured_admin_qq: str
    ) -> dict:
        return self.request_with_headers(
            "GET",
            f"/api/v1/bridge/admin/worlds/{world_id}/quota",
            headers={
                "X-Connector-Token": self.token,
                "X-Admin-QQ": sender_qq_id,
                "X-Configured-Admin-QQ": configured_admin_qq,
            },
        )

    def admin_world_nodes(
        self,
        world_id: str,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
        before_sequence: int | None = None,
        limit: int = 10,
    ) -> dict:
        query_values = {"limit": limit}
        if before_sequence is not None:
            query_values["before_sequence"] = before_sequence
        query = urlencode(query_values)
        return self.request_with_headers(
            "GET",
            f"/api/v1/bridge/admin/worlds/{world_id}/nodes?{query}",
            headers={
                "X-Connector-Token": self.token,
                "X-Admin-QQ": sender_qq_id,
                "X-Configured-Admin-QQ": configured_admin_qq,
            },
        )

    def admin_rollback_world(
        self,
        world_id: str,
        node_id: str,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
    ) -> dict:
        return self.request_with_headers(
            "POST",
            f"/api/v1/bridge/admin/worlds/{world_id}/rollback",
            {
                "sender_qq_id": sender_qq_id,
                "configured_admin_qq": configured_admin_qq,
                "node_id": node_id,
            },
            headers={"X-Connector-Token": self.token},
        )

    def admin_world_summary(self, *, sender_qq_id: str, configured_admin_qq: str) -> dict:
        return self.request_with_headers(
            "GET",
            "/api/v1/bridge/admin/summary",
            headers={
                "X-Connector-Token": self.token,
                "X-Admin-QQ": sender_qq_id,
                "X-Configured-Admin-QQ": configured_admin_qq,
            },
        )

    def admin_pending_interactions(self, *, sender_qq_id: str, configured_admin_qq: str) -> dict:
        return self.request_with_headers(
            "GET",
            "/api/v1/bridge/admin/interactions",
            headers={
                "X-Connector-Token": self.token,
                "X-Admin-QQ": sender_qq_id,
                "X-Configured-Admin-QQ": configured_admin_qq,
            },
        )

    def admin_audit_summary(
        self,
        *,
        sender_qq_id: str,
        configured_admin_qq: str,
        limit: int = 10,
    ) -> dict:
        query = urlencode({"limit": limit})
        return self.request_with_headers(
            "GET",
            f"/api/v1/bridge/admin/audit?{query}",
            headers={
                "X-Connector-Token": self.token,
                "X-Admin-QQ": sender_qq_id,
                "X-Configured-Admin-QQ": configured_admin_qq,
            },
        )

    def action(self, world_id: str, content: str, *, request_id: str | None = None, expected_version: int | None = None) -> dict:
        body: dict = {"content": content}
        if request_id:
            body["request_id"] = request_id
        if expected_version is not None:
            body["expected_version"] = expected_version
        return self.request("POST", f"/api/v1/worlds/{world_id}/actions", body)

    def chat(self, world_id: str, content: str, *, request_id: str | None = None) -> dict:
        body: dict = {"content": content}
        if request_id:
            body["request_id"] = request_id
        return self.request("POST", f"/api/v1/worlds/{world_id}/chat", body)

    def bridge_poll(self) -> dict:
        return self.request_with_headers("GET", "/api/v1/bridge/poll", headers={"X-Connector-Token": self.token})

    def bridge_connect(self, *, default_world_id: str = "") -> BridgeWebSocket:
        if not self.endpoint:
            raise ConnectorError("尚未配置虚拟域服务端地址")
        if not self.token:
            raise ConnectorError("尚未配置 AstrBot 连接凭证")
        parts = urlsplit(self.endpoint)
        secure = parts.scheme.lower() == "https"
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            raise ConnectorError("服务端地址必须使用 http:// 或 https://")
        port = parts.port or (443 if secure else 80)
        path = parts.path.rstrip("/") + "/api/v1/bridge/ws"
        host_header = parts.netloc
        connection = socket.create_connection((parts.hostname, port), timeout=15)
        try:
            if secure:
                context = self._tls_context()
                connection = context.wrap_socket(connection, server_hostname=parts.hostname)
            connection.settimeout(35)
            key = base64.b64encode(os.urandom(16)).decode("ascii")
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"X-Connector-Token: {self.token}\r\n\r\n"
            ).encode("ascii")
            connection.sendall(request)
            response = connection.makefile("rb")
            status = response.readline(4096).decode("latin-1").strip()
            headers = {}
            while True:
                line = response.readline(4096)
                if not line or line in {b"\r\n", b"\n"}:
                    break
                if b":" in line:
                    name, value = line.decode("latin-1").split(":", 1)
                    headers[name.strip().lower()] = value.strip()
            response.close()
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
            if " 101 " not in f" {status} " or headers.get("sec-websocket-accept") != expected:
                raise ConnectorError(f"虚拟域 WebSocket 握手失败：{status or '无响应'}")
            websocket = BridgeWebSocket(connection)
            websocket.send_json({
                "type": "hello",
                "protocol_version": 1,
                "plugin_version": "0.1.0",
                "default_world_id": default_world_id,
                "capabilities": ["full_astrbot_event", "request_response", "qq_admin_commands"],
            })
            ready = websocket.receive_json()
            if ready.get("type") != "ready" or ready.get("protocol_version") != 1:
                raise ConnectorError("虚拟域服务端 WebSocket 协议版本不匹配")
            return websocket
        except Exception:
            connection.close()
            raise

    def bridge_respond(self, request_id: str, response: dict) -> dict:
        return self.request_with_headers(
            "POST",
            "/api/v1/bridge/respond",
            {"request_id": request_id, "response": response},
            headers={"X-Connector-Token": self.token},
        )

    def request_with_headers(self, method: str, path: str, body: dict | None = None, *, headers: dict[str, str] | None = None) -> dict:
        if not self.endpoint:
            raise ConnectorError("尚未配置虚拟域服务端地址")
        merged = {"Accept": "application/json", **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            merged["Content-Type"] = "application/json"
        request = Request(f"{self.endpoint}{path}", data=data, headers=merged, method=method)
        try:
            context = self._tls_context()
            opener_options = {"context": context} if context is not None else {}
            with urlopen(request, timeout=30, **opener_options) as response:
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                result = json.loads(exc.read().decode("utf-8"))
            except Exception:
                result = {"message": str(exc)}
            finally:
                exc.close()
            detail = result.get("detail", result) if isinstance(result, dict) else result
            message = detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail)
            raise ConnectorError(message) from exc
        except URLError as exc:
            raise ConnectorError(f"连接虚拟域失败：{exc.reason}") from exc
        if not isinstance(result, dict):
            raise ConnectorError("虚拟域返回了无效数据")
        return result
