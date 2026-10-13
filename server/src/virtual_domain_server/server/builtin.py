"""不依赖第三方包的开发服务器。

它覆盖客户端和连接器首版所需的 REST 接口，生产部署安装可选依赖后建议
使用 FastAPI/Uvicorn；保留该实现是为了让本地首次启动不必先准备 Python
环境，也方便插件开发阶段做端到端测试。
"""

from __future__ import annotations

import json
import base64
import hashlib
import secrets
import socket
import threading
import logging
import ssl
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..core.model_settings import ModelSettingsManager
from ..core.secret_store import EncryptedConfigStore
from ..core.models import Account, WorldMode, dataclass_dict
from ..core.service import AuthenticationError, DomainError, PermissionDenied, VirtualDomainService
from ..core.simulation import WorldSimulationEngine
from ..core.bot_bridge import WebSocketBotBridge, receive_websocket_frame, send_websocket_frame


class GracefulThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = False


class ApiHandler(BaseHTTPRequestHandler):
    service: VirtualDomainService
    bot_bridge: Any = None
    model_settings: ModelSettingsManager | None = None

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/api/v1/bridge/ws":
            self._serve_bridge_websocket()
            return
        self._dispatch_scoped("GET")

    def _serve_bridge_websocket(self) -> None:
        bridge = self.bot_bridge
        if not isinstance(bridge, WebSocketBotBridge):
            return self._send(404, {"code": "not_found", "message": "接口不存在"})
        if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
            return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
        key = self.headers.get("Sec-WebSocket-Key", "")
        if (
            self.headers.get("Upgrade", "").lower() != "websocket"
            or "upgrade" not in {item.strip().lower() for item in self.headers.get("Connection", "").split(",")}
            or self.headers.get("Sec-WebSocket-Version") != "13"
        ):
            return self._send(400, {"code": "invalid_websocket_handshake", "message": "WebSocket 握手无效"})
        try:
            decoded_key = base64.b64decode(key, validate=True)
        except ValueError:
            decoded_key = b""
        if len(decoded_key) != 16:
            return self._send(400, {"code": "invalid_websocket_key", "message": "WebSocket 握手密钥无效"})
        if not bridge.attach(self.connection):
            return self._send(409, {"code": "connector_already_connected", "message": "AstrBot 连接器已连接"})

        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        connection = self.connection
        try:
            self.send_response_only(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            connection.settimeout(20)
            opcode, raw_hello = receive_websocket_frame(connection, expect_masked=True)
            hello = json.loads(raw_hello.decode("utf-8")) if opcode == 1 else {}
            if hello.get("type") != "hello" or hello.get("protocol_version") != 1:
                send_websocket_frame(connection, 8, (1008).to_bytes(2, "big"))
                return
            info = {
                "plugin_version": str(hello.get("plugin_version", ""))[:64],
                "default_world_id": str(hello.get("default_world_id", ""))[:128],
                "capabilities": [str(item)[:64] for item in hello.get("capabilities", [])[:32]]
                if isinstance(hello.get("capabilities", []), list) else [],
            }
            bridge.set_client_info(connection, info)
            bridge.send_json(connection, {"type": "ready", "protocol_version": 1, "server_version": "0.1.0", "bot_mode": self.service.bot_mode})
            last_seen = time.monotonic()
            while True:
                try:
                    opcode, payload = receive_websocket_frame(connection, expect_masked=True)
                except socket.timeout:
                    if time.monotonic() - last_seen > 60:
                        break
                    with bridge.socket_send_lock:
                        send_websocket_frame(connection, 9, b"vd")
                    continue
                last_seen = time.monotonic()
                if opcode == 8:
                    with bridge.socket_send_lock:
                        send_websocket_frame(connection, 8, payload[:125])
                    break
                if opcode == 9:
                    with bridge.socket_send_lock:
                        send_websocket_frame(connection, 10, payload[:125])
                    continue
                if opcode == 10:
                    continue
                if opcode != 1:
                    send_websocket_frame(connection, 8, (1003).to_bytes(2, "big"))
                    break
                try:
                    message = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    send_websocket_frame(connection, 8, (1007).to_bytes(2, "big"))
                    break
                if isinstance(message, dict) and message.get("type") == "response":
                    bridge.respond_message(message)
                elif isinstance(message, dict) and message.get("type") == "ping":
                    bridge.send_json(connection, {"type": "pong"})
        except (ConnectionError, OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            bridge.detach(connection)

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch_scoped("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch_scoped("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch_scoped("DELETE")

    def _dispatch_scoped(self, method: str) -> None:
        with self.service.store.connection_scope():
            self._dispatch(method)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if method == "GET" and path == "/api/v1/health":
                return self._send(200, {"status": "ok", "service": "virtual-domain", "version": "0.1.0", "bot_mode": self.service.bot_mode, "bot_connected": bool(self.bot_bridge and self.bot_bridge.connected), "world_model": self.service.simulation_engine.provider.config.model if self.service.simulation_engine else None})
            if path == "/api/v1/bridge/worlds" and method == "GET":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                return self._send(200, {"worlds": self.service.connector_worlds()})
            if path == "/api/v1/bridge/admin/summary" and method == "GET":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                return self._send(
                    200,
                    self.service.connector_admin_world_summary(
                        self.headers.get("X-Admin-QQ", ""),
                        self.headers.get("X-Configured-Admin-QQ", ""),
                    ),
                )
            if path == "/api/v1/bridge/admin/interactions" and method == "GET":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                return self._send(
                    200,
                    self.service.connector_admin_pending_interactions(
                        self.headers.get("X-Admin-QQ", ""),
                        self.headers.get("X-Configured-Admin-QQ", ""),
                    ),
                )
            if path == "/api/v1/bridge/admin/audit" and method == "GET":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                raw_limit = parse_qs(parsed.query).get("limit", ["10"])[0]
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError):
                    return self._send(400, {"code": "invalid_audit_limit", "message": "审计记录条数必须是 1 到 20 之间的整数"})
                result = self.service.connector_admin_audit_summary(
                    self.headers.get("X-Admin-QQ", ""),
                    self.headers.get("X-Configured-Admin-QQ", ""),
                    limit=limit,
                )
                return self._send(200, result)
            if (
                path.startswith("/api/v1/bridge/admin/worlds/")
                and path.endswith("/nodes")
                and method == "GET"
            ):
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/nodes").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                query = parse_qs(parsed.query)
                try:
                    raw_limit = query.get("limit", ["10"])[0]
                    limit = int(raw_limit)
                    raw_before = query.get("before_sequence", [None])[0]
                    before_sequence = int(raw_before) if raw_before is not None else None
                except (TypeError, ValueError):
                    return self._send(400, {"code": "invalid_node_cursor", "message": "节点分页参数必须是非负整数"})
                if not 1 <= limit <= 20 or (before_sequence is not None and before_sequence < 0):
                    return self._send(400, {"code": "invalid_node_page", "message": "节点分页数量须为 1 到 20，分页位置须为非负整数"})
                result = self.service.connector_admin_world_nodes(
                    self.headers.get("X-Admin-QQ", ""),
                    self.headers.get("X-Configured-Admin-QQ", ""),
                    world_id,
                    before_sequence=before_sequence,
                    limit=limit,
                )
                return self._send(200, result)
            if path == "/api/v1/bridge/qq/bind" and method == "POST":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                body = self._json()
                self.service.bind_qq(
                    str(body.get("qq_id", "")),
                    str(body.get("code", "")),
                    session_origin=body.get("session_origin"),
                )
                return self._send(200, {"ok": True})
            if path.startswith("/api/v1/bridge/admin/worlds/") and path.endswith("/invitations") and method == "POST":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/invitations").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                body = self._json()
                hours = body.get("hours", 24)
                if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 24 * 30:
                    return self._send(400, {"code": "invalid_invitation_expiry", "message": "邀请有效期必须是 1 到 720 小时"})
                invitation = self.service.connector_create_invitation(
                    str(body.get("sender_qq_id", "")),
                    str(body.get("configured_admin_qq", "")),
                    world_id,
                    hours=hours,
                )
                return self._send(201, invitation)
            if path.startswith("/api/v1/bridge/admin/worlds/") and path.endswith("/invitations") and method == "DELETE":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/invitations").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                body = self._json()
                result = self.service.connector_revoke_invitation(
                    str(body.get("sender_qq_id", "")),
                    str(body.get("configured_admin_qq", "")),
                    world_id,
                    str(body.get("code", "")),
                )
                return self._send(200, result)
            if path.startswith("/api/v1/bridge/admin/worlds/") and path.endswith("/current") and method == "POST":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/current").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                body = self._json()
                confirmed = body.get("confirm_online_disconnect", False)
                if not isinstance(confirmed, bool):
                    return self._send(400, {"code": "invalid_world_switch_confirmation", "message": "confirm_online_disconnect 必须是布尔值"})
                world = self.service.connector_set_current_world(
                    str(body.get("sender_qq_id", "")),
                    str(body.get("configured_admin_qq", "")),
                    world_id,
                    confirm_online_disconnect=confirmed,
                )
                return self._send(200, {"ok": True, "world_id": world.id, "status": world.status.value, "current": world.current})
            if path.startswith("/api/v1/bridge/admin/worlds/") and path.endswith("/state") and method == "POST":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/state").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                body = self._json()
                paused = body.get("paused", True)
                if not isinstance(paused, bool):
                    return self._send(400, {"code": "invalid_world_status", "message": "paused 必须是布尔值"})
                world = self.service.connector_pause_world(
                    str(body.get("sender_qq_id", "")),
                    str(body.get("configured_admin_qq", "")),
                    world_id,
                    paused,
                )
                return self._send(200, {"ok": True, "world_id": world.id, "status": world.status.value})
            if (
                path.startswith("/api/v1/bridge/admin/worlds/")
                and path.endswith("/rollback")
                and method == "POST"
            ):
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/rollback").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                body = self._json()
                node_id = str(body.get("node_id", ""))
                if not node_id or len(node_id) > 64:
                    return self._send(400, {"code": "invalid_world_node", "message": "必须选择一个有效世界节点"})
                result = self.service.connector_admin_rollback_world(
                    str(body.get("sender_qq_id", "")),
                    str(body.get("configured_admin_qq", "")),
                    world_id,
                    node_id,
                )
                return self._send(200, result)
            if path.startswith("/api/v1/bridge/admin/worlds/") and path.endswith("/delete") and method == "POST":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/delete").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                body = self._json()
                result = self.service.connector_delete_world(
                    str(body.get("sender_qq_id", "")),
                    str(body.get("configured_admin_qq", "")),
                    world_id,
                    str(body.get("confirmed_world_name", "")),
                )
                return self._send(200, result)
            if path.startswith("/api/v1/bridge/admin/worlds/") and path.endswith("/quota") and method == "GET":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                world_id = path.removeprefix("/api/v1/bridge/admin/worlds/").removesuffix("/quota").strip("/")
                if not world_id or "/" in world_id:
                    return self._send(404, {"code": "world_not_found", "message": "世界不存在"})
                return self._send(200, self.service.connector_quota_snapshot(
                    self.headers.get("X-Admin-QQ", ""),
                    self.headers.get("X-Configured-Admin-QQ", ""),
                    world_id,
                ))
            if path == "/api/v1/bridge/admin/backups" and method in {"GET", "POST"}:
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                if method == "POST":
                    body = self._json()
                else:
                    query = parse_qs(parsed.query)
                    body = {
                        "sender_qq_id": query.get("sender_qq_id", [""])[0],
                        "configured_admin_qq": query.get("configured_admin_qq", [""])[0],
                    }
                sender_qq_id = str(body.get("sender_qq_id", ""))
                configured_admin_qq = str(body.get("configured_admin_qq", ""))
                if method == "POST":
                    backup = self.service.connector_create_backup(sender_qq_id, configured_admin_qq)
                    return self._send(201, backup)
                return self._send(200, {"backups": self.service.connector_list_backups(sender_qq_id, configured_admin_qq)})
            if path == "/api/v1/bridge/poll" and method == "GET":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                item = bridge.poll(timeout=20)
                return self._send(200, {"request": item})
            if path == "/api/v1/bridge/respond" and method == "POST":
                bridge = self.bot_bridge
                if bridge is None:
                    return self._send(409, {"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
                if not bridge.token or not secrets.compare_digest(self.headers.get("X-Connector-Token", ""), bridge.token):
                    return self._send(401, {"code": "invalid_connector_token", "message": "连接凭证错误"})
                body = self._json()
                accepted = bridge.respond(str(body.get("request_id", "")), body.get("response") or {})
                return self._send(200, {"ok": accepted})
            if path == "/api/v1/auth/register" and method == "POST":
                body = self._json()
                acc, token, world_id = self.service.register_with_credential(
                    body.get("username", ""),
                    body.get("password", ""),
                    initialization_code=body.get("initialization_code"),
                    invitation_code=body.get("invitation_code"),
                    display_name=body.get("display_name"),
                )
                return self._send(201, {"account": dataclass_dict(acc), "token": token, "world_id": world_id})
            if path == "/api/v1/auth/login" and method == "POST":
                body = self._json()
                acc, token = self.service.login(body.get("username", ""), body.get("password", ""))
                return self._send(200, {"account": dataclass_dict(acc), "token": token})
            acc = self._account()
            if path == "/api/v1/auth/management" and method == "POST":
                body = self._json()
                token = self.service.issue_management_token(acc, str(body.get("pairing_code", "")))
                return self._send(200, {"management_token": token})
            if path == "/api/v1/auth/management" and method == "DELETE":
                token = self.headers.get("X-Management-Credential", "").strip()
                self._require_management(acc)
                self.service.revoke_management_token(acc, token)
                return self._send(200, {"ok": True})

            manager = None
            management_credential = self.headers.get("X-Management-Credential", "").strip()
            if management_credential:
                manager = self._management_account()
                if manager.id != acc.id:
                    raise PermissionDenied("创作连接与当前登录账号不匹配", code="management_account_mismatch")
            if self._requires_management(method, path) and manager is None:
                raise PermissionDenied("切换创作模式需要服务端管理员连接凭证", code="management_connection_required")
            # A normal login for the owner account remains a player connection. Only
            # the separate management credential unlocks the owner's admin view.
            if manager is None and acc.is_admin:
                acc = Account(acc.id, acc.username, False, acc.created_at)
            if path == "/api/v1/auth/me/qq-binding" and method == "GET":
                return self._send(200, self.service.qq_binding_status(acc))
            if path == "/api/v1/auth/me/qq-binding-code" and method == "POST":
                return self._send(200, self.service.issue_qq_binding_code(acc))
            if path == "/api/v1/auth/me/qq-binding" and method == "DELETE":
                body = self._json()
                self.service.unbind_qq(acc, str(body.get("current_password", "")))
                return self._send(200, {"ok": True, "bound": False})
            if path == "/api/v1/auth/me" and method == "PATCH":
                body = self._json()
                updated = self.service.update_account(
                    acc,
                    username=body.get("username"),
                    current_password=body.get("current_password"),
                    new_password=body.get("new_password"),
                )
                return self._send(200, dataclass_dict(updated))
            if path == "/api/v1/worlds" and method == "GET":
                return self._send(200, {"worlds": [self.service.client_world(acc, item, management=manager is not None) for item in self.service.list_worlds(acc)]})
            if path == "/api/v1/worlds/generate" and method == "POST":
                body = self._json()
                result = self.service.generate_worldview(
                    acc,
                    str(body.get("prompt", "")),
                    request_id=str(body.get("request_id", "")),
                    target_world_id=str(body["world_id"]) if body.get("world_id") else None,
                )
                return self._send(200, result)
            if path == "/api/v1/worlds" and method == "POST":
                body = self._json()
                world = self.service.create_world(
                    acc,
                    body.get("name", ""),
                    mode=WorldMode(body.get("mode", "static")),
                    settings=body.get("settings") or {},
                    state=body.get("state") or {},
                    generation_request_id=body.get("generation_request_id"),
                )
                return self._send(201, dataclass_dict(world))
            if path == "/api/v1/admin/accounts" and method == "GET":
                return self._send(200, {"accounts": self.service.admin_accounts(acc)})
            if path == "/api/v1/admin/model-profiles" and method == "GET":
                if self.model_settings is None:
                    return self._send(503, {"code": "model_settings_unavailable", "message": "模型配置管理未初始化"})
                return self._send(200, self.model_settings.public_settings())
            if path == "/api/v1/admin/model-profiles" and method == "PATCH":
                if self.model_settings is None:
                    return self._send(503, {"code": "model_settings_unavailable", "message": "模型配置管理未初始化"})
                body = self._json()
                role = str(body.get("role", ""))
                config = body.get("config")
                if role == "memory" and isinstance(config, dict):
                    changed_fields = sorted({
                        field
                        for part in config.values()
                        if isinstance(part, dict)
                        for field in part
                    })
                    api_key_changed = any(
                        bool(str(part.get("api_key", "")).strip())
                        for part in config.values()
                        if isinstance(part, dict)
                    )
                else:
                    changed_fields = sorted(config) if isinstance(config, dict) else []
                    api_key_changed = bool(
                        str(config.get("api_key", "")).strip()
                    ) if isinstance(config, dict) else False
                result = self.model_settings.update(body)
                self.service.record_model_settings_update(
                    acc,
                    role,
                    changed_fields=changed_fields,
                    api_key_changed=api_key_changed or bool(body.get("clear_api_key")),
                )
                return self._send(200, result)
            if path == "/api/v1/admin/bot-mode" and method == "POST":
                if self.model_settings is None:
                    return self._send(503, {"code": "model_settings_unavailable", "message": "模型配置管理未初始化"})
                body = self._json()
                try:
                    result = self.model_settings.switch_bot_mode(
                        acc,
                        str(body.get("bot_mode", "")),
                        confirmed=body.get("confirmed") is True,
                    )
                except ValueError as exc:
                    return self._send(400, {"code": "invalid_bot_mode", "message": str(exc)})
                return self._send(200, result)
            if path.startswith("/api/v1/admin/accounts/") and path.endswith("/reset-password") and method == "POST":
                target_id = path[len("/api/v1/admin/accounts/"):-len("/reset-password")].strip("/")
                body = self._json()
                self.service.reset_account_password(acc, target_id, str(body.get("new_password", "")))
                return self._send(200, {"ok": True})
            if path == "/api/v1/admin/world-generation-usage" and method == "GET":
                return self._send(200, self.service.service_generation_usage(acc))
            if path == "/api/v1/admin/backups/settings" and method == "GET":
                return self._send(200, self.service.backup_configuration(acc))
            if path == "/api/v1/admin/backups/settings" and method == "POST":
                body = self._json()
                return self._send(200, self.service.set_backup_configuration(acc, automatic_enabled=body.get("automatic_enabled", False), interval_hours=body.get("interval_hours", 24), retention_count=body.get("retention_count")))
            if path == "/api/v1/admin/backups" and method == "GET":
                return self._send(200, {"backups": self.service.list_backups(acc)})
            if path == "/api/v1/admin/backups" and method == "POST":
                return self._send(201, self.service.create_backup(acc, kind="manual"))
            if path == "/api/v1/admin/backups/import" and method == "POST":
                body = self._json()
                snapshot = body.get("snapshot") if isinstance(body, dict) and "snapshot" in body else body
                mappings = body.get("account_mappings") if isinstance(body, dict) else None
                return self._send(200, self.service.restore_backup(acc, snapshot, account_mappings=mappings))
            if path == "/api/v1/admin/backups/import/preview" and method == "POST":
                body = self._json()
                snapshot = body.get("snapshot") if isinstance(body, dict) and "snapshot" in body else body
                return self._send(200, self.service.preview_backup_import(acc, snapshot))
            backup_parts = path.split("/")
            if len(backup_parts) == 7 and backup_parts[:4] == ["", "api", "v1", "admin"] and backup_parts[4] == "backups":
                backup_id = backup_parts[5]
                if backup_parts[6] == "download" and method == "GET":
                    file_path, file_name = self.service.backup_download_path(acc, backup_id)
                    return self._send_file(file_path, file_name)
                if backup_parts[6] == "restore" and method == "POST":
                    return self._send(200, self.service.restore_backup(acc, backup_id))
            if len(backup_parts) == 6 and backup_parts[:4] == ["", "api", "v1", "admin"] and backup_parts[4] == "backups" and method == "DELETE":
                self.service.delete_backup(acc, backup_parts[5])
                return self._send(200, {"ok": True})
            if path == "/api/v1/worlds/join-by-invite" and method == "POST":
                body = self._json()
                world_id = self.service.redeem_invitation(
                    acc,
                    str(body.get("invite_code", "")),
                    display_name=body.get("display_name"),
                )
                return self._send(200, {"world_id": world_id})
            if path == "/api/v1/admin/audit" and method == "GET":
                limit = int(parse_qs(parsed.query).get("limit", [100])[0])
                return self._send(200, {"entries": self.service.audit_logs(acc, limit=limit)})
            parts = path.split("/")
            if len(parts) >= 5 and parts[:4] == ["", "api", "v1", "worlds"]:
                world_id = parts[4]
                if len(parts) == 5 and method == "GET":
                    return self._send(200, self.service.client_world(acc, self.service.get_world(acc, world_id), management=manager is not None))
                if len(parts) == 5 and method == "DELETE":
                    return self._send(200, self.service.delete_world(manager, world_id))
                if len(parts) < 6:
                    return self._send(404, {"code": "not_found", "message": "接口不存在"})
                action = parts[5]
                if action == "nodes" and method == "GET":
                    return self._send(200, {"nodes": self.service.world_nodes(manager, world_id)})
                if action == "rollback" and method == "POST":
                    body = self._json()
                    return self._send(
                        200,
                        self.service.rollback_world(manager, world_id, str(body.get("node_id", ""))),
                    )
                if action == "configuration" and method == "GET":
                    return self._send(200, self.service.world_configuration(acc, world_id))
                if action == "configuration" and method == "PATCH":
                    body = self._json()
                    result = self.service.stage_world_configuration(acc, world_id, settings=body.get("settings") or {}, mode=body.get("mode"))
                    return self._send(200, result)
                if (
                    action == "configuration"
                    and len(parts) == 7
                    and parts[6] == "apply-once"
                    and method == "POST"
                ):
                    body = self._json()
                    event = self.service.apply_pending_configuration_once(
                        acc,
                        world_id,
                        request_id=str(body.get("request_id", "")),
                    )
                    return self._send(200, dataclass_dict(event))
                if (
                    action == "admin"
                    and len(parts) == 7
                    and parts[6] == "interactions"
                    and method == "GET"
                ):
                    return self._send(200, self.service.admin_world_interactions(manager, world_id))
                if action == "presence" and method == "POST":
                    return self._send(200, self.service.touch_presence(acc, world_id, active=self._json().get("active", False)))
                if action == "runtime" and method == "GET":
                    return self._send(200, self.service.runtime_status(acc, world_id))
                if action == "leave" and method == "POST":
                    self.service.leave_world(acc, world_id)
                    return self._send(200, {"ok": True})
                if action == "current" and method == "POST":
                    body = self._json()
                    confirmed = body.get("confirm_online_disconnect", False)
                    if not isinstance(confirmed, bool):
                        return self._send(400, {"code": "invalid_world_switch_confirmation", "message": "confirm_online_disconnect 必须是布尔值"})
                    return self._send(200, dataclass_dict(self.service.set_current_world(acc, world_id, confirm_online_disconnect=confirmed)))
                if action == "pause" and method == "POST":
                    return self._send(200, dataclass_dict(self.service.pause_world(acc, world_id, True)))
                if action == "resume" and method == "POST":
                    return self._send(200, dataclass_dict(self.service.pause_world(acc, world_id, False)))
                if action == "join" and method == "POST":
                    body = self._json()
                    if body.get("invite_code"):
                        self.service.redeem_invitation(acc, body["invite_code"], display_name=body.get("display_name"), expected_world_id=world_id)
                    else:
                        self.service.join_world(acc, world_id, display_name=body.get("display_name"))
                    return self._send(200, {"ok": True})
                if action == "invitations" and method == "POST":
                    result = self.service.create_invitation(acc, world_id, hours=int((self._json()).get("hours", 24)))
                    return self._send(201, {"code": result[0], "expires_at": result[1]})
                if action == "invitations" and method == "DELETE":
                    result = self.service.revoke_invitation(acc, world_id, str(self._json().get("code", "")))
                    return self._send(200, result)
                if action == "interactions" and method == "GET":
                    return self._send(200, self.service.interactions(acc, world_id))
                if action == "interactions" and len(parts) == 8 and parts[7] == "response" and method == "POST":
                    body = self._json()
                    return self._send(200, self.service.respond_interaction(acc, world_id, parts[6], str(body.get("content", ""))))
                if action == "events" and method == "GET":
                    after = int(parse_qs(parsed.query).get("after", [0])[0])
                    return self._send(200, {"events": [dataclass_dict(item) for item in self.service.events(acc, world_id, after_sequence=after, management=manager is not None)]})
                if action in ("actions", "chat") and method == "POST":
                    body = self._json()
                    if action == "actions":
                        event = self.service.submit_action(acc, world_id, body.get("content", ""), request_id=body.get("request_id"), expected_version=body.get("expected_version"), actual_tokens=int(body.get("actual_tokens", 0)))
                    else:
                        event = self.service.submit_chat(acc, world_id, body.get("content", ""), request_id=body.get("request_id"), actual_tokens=int(body.get("actual_tokens", 0)))
                    return self._send(200, dataclass_dict(event) if hasattr(event, "__dataclass_fields__") else event)
                if len(parts) == 6 and action == "quota" and method == "GET":
                    self.service.get_world(acc, world_id)
                    return self._send(200, self.service.quota_snapshot(world_id).as_dict())
                if len(parts) == 6 and action == "quota" and method == "POST":
                    return self._send(200, self.service.set_quota_limits(acc, world_id, **self._json()).as_dict())
                if len(parts) == 6 and action == "usage" and method == "POST":
                    body = self._json()
                    return self._send(200, self.service.record_usage(acc, world_id, str(body.get("ledger", "")), int(body.get("actual_tokens", 0))).as_dict())
            return self._send(404, {"code": "not_found", "message": "接口不存在"})
        except DomainError as exc:
            return self._send(exc.status_code, {"code": exc.code, "message": str(exc)})
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            return self._send(400, {"code": "invalid_request", "message": str(exc)})

    @staticmethod
    def _requires_management(method: str, path: str) -> bool:
        if path.startswith("/api/v1/admin/"):
            return True
        if (
            path.startswith("/api/v1/worlds/")
            and path.endswith("/configuration/apply-once")
            and method == "POST"
        ):
            return True
        parts = path.split("/")
        if (
            len(parts) == 5
            and parts[:4] == ["", "api", "v1", "worlds"]
            and method == "DELETE"
        ):
            return True
        if (
            len(parts) == 7
            and parts[:4] == ["", "api", "v1", "worlds"]
            and parts[5:] == ["admin", "interactions"]
            and method == "GET"
        ):
            return True
        if path == "/api/v1/worlds" and method == "POST":
            return True
        if path == "/api/v1/worlds/generate" and method == "POST":
            return True
        if len(parts) == 6 and parts[:4] == ["", "api", "v1", "worlds"]:
            action = parts[5]
            if action in {"current", "pause", "resume", "invitations"}:
                return method in {"POST", "DELETE"}
            if action == "nodes":
                return method == "GET"
            if action == "rollback":
                return method == "POST"
            if action == "configuration":
                return method in {"GET", "PATCH"}
            if action == "quota":
                return method == "POST"
        return False

    def _management_account(self):
        token = self.headers.get("X-Management-Credential", "").strip()
        if not token:
            raise PermissionDenied("切换创作模式需要服务端管理员连接凭证", code="management_connection_required")
        return self.service.management_account_from_token(token)

    def _require_management(self, account):
        manager = self._management_account()
        if manager.id != account.id:
            raise PermissionDenied("创作连接与当前登录账号不匹配", code="management_account_mismatch")
        return manager

    def _account(self):
        auth = self.headers.get("Authorization", "")
        if not auth.lower().startswith("bearer "):
            raise AuthenticationError("需要 Bearer 登录令牌", code="authentication_required")
        return self.service.account_from_token(auth[7:].strip())

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _send_file(self, path, file_name: str):
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        safe_name = file_name.replace('"', "")
        self.send_header("Content-Disposition", f'attachment; filename="{safe_name}"')
        self.end_headers()
        self.wfile.write(raw)

    def _send(self, status: int, body: Any):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def create_server_tls_context(
    certfile: str | None = None,
    keyfile: str | None = None,
    client_ca_file: str | None = None,
    client_crl_file: str | None = None,
) -> ssl.SSLContext | None:
    if bool(certfile) != bool(keyfile):
        raise ValueError("TLS 服务端证书和私钥必须同时配置")
    if client_ca_file and not certfile:
        raise ValueError("启用双向 TLS 前必须配置服务端证书和私钥")
    if client_crl_file and not client_ca_file:
        raise ValueError("启用客户端证书吊销检查前必须配置客户端 CA")
    if not certfile:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile, keyfile)
    if client_ca_file:
        context.load_verify_locations(cafile=client_ca_file)
        context.verify_mode = ssl.CERT_REQUIRED
    if client_crl_file:
        context.load_verify_locations(cafile=client_crl_file)
        if not hasattr(ssl, "VERIFY_CRL_CHECK_LEAF"):
            raise ValueError("当前 Python/OpenSSL 不支持客户端证书 CRL 检查")
        context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    return context


def serve(
    host: str,
    port: int,
    db_path: str,
    *,
    simulation_engine: WorldSimulationEngine | None = None,
    bot_bridge: Any = None,
    bot_mode: str = "standalone",
    memory_configs: tuple[dict, dict] | None = None,
    model_settings_dir: str | None = None,
    tls_certfile: str | None = None,
    tls_keyfile: str | None = None,
    tls_client_ca_file: str | None = None,
    tls_client_crl_file: str | None = None,
) -> None:
    tls_context = create_server_tls_context(
        tls_certfile,
        tls_keyfile,
        tls_client_ca_file,
        tls_client_crl_file,
    )
    service = VirtualDomainService(db_path, simulation_engine=simulation_engine, bot_bridge=bot_bridge, bot_mode=bot_mode, memory_configs=memory_configs)
    model_settings = None
    profiles_path = (
        Path(model_settings_dir) / "model-profiles.enc"
        if model_settings_dir
        else None
    )
    if model_settings_dir and (
        simulation_engine is not None
        or memory_configs is not None
        or bot_bridge is not None
        or (profiles_path is not None and profiles_path.is_file())
    ):
        model_settings = ModelSettingsManager(
            service,
            EncryptedConfigStore(model_settings_dir),
            bot_mode=bot_mode,
        )
    if service.initialization_code:
        print("首次初始化管理员账号时，请使用本机一次性初始化码（仅显示在服务端控制台）：")
        print(service.initialization_code)
    if service.management_pairing_code:
        print("客户端连接创作模式时，需要本机管理员配对码（仅显示在服务端控制台）：")
        print(service.management_pairing_code)
    handler = type(
        "VirtualDomainApiHandler",
        (ApiHandler,),
        {
            "service": service,
            "bot_bridge": bot_bridge,
            "model_settings": model_settings,
        },
    )
    server = GracefulThreadingHTTPServer((host, port), handler)
    if tls_context:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    stopped = threading.Event()

    def offline_worker():
        while not stopped.wait(10):
            try:
                service.backup_tick()
                service.tick()
            except Exception:
                logging.getLogger("virtual_domain").exception("Offline opportunity failed")

    worker = threading.Thread(target=offline_worker, name="virtual-domain-offline", daemon=True)
    worker.start()
    try:
        scheme = "https" if tls_context else "http"
        print(f"虚拟域服务端已启动：{scheme}://{host}:{port}")
        if tls_client_ca_file:
            print("双向 TLS 已启用：仅接受由指定 CA 签发的客户端证书。")
        if tls_client_crl_file:
            print("客户端证书 CRL 吊销检查已启用；更新 CRL 后需重启服务端。")
        server.serve_forever()
    finally:
        stopped.set()
        server.server_close()
        worker.join(timeout=3)
        if not worker.is_alive():
            service.close()

