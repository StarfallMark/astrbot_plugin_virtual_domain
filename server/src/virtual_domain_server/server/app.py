"""FastAPI 适配层（可选依赖）。"""

from typing import Any
import secrets
from contextvars import ContextVar

from ..core.models import Account, WorldMode, dataclass_dict
from ..core.service import DomainError, VirtualDomainService


def create_app(
    service: VirtualDomainService | None = None,
    model_settings: Any = None,
) -> Any:
    """创建 FastAPI 应用。

    FastAPI 作为可选依赖，未安装时仍可使用同包提供的标准库服务器。
    """

    try:
        from fastapi import FastAPI, Header, HTTPException, Query, Request, WebSocket
        from fastapi.responses import FileResponse, JSONResponse
        from pydantic import BaseModel, Field
    except ImportError as exc:  # pragma: no cover - 由安装方式决定
        raise RuntimeError("FastAPI 服务器需要安装 virtual-domain[server]") from exc

    domain = service or VirtualDomainService()
    app = FastAPI(title="虚拟域服务端", version="0.1.0")
    management_authorized: ContextVar[bool] = ContextVar("virtual_domain_management_authorized", default=False)

    def requires_management(method: str, path: str) -> bool:
        if path.startswith("/api/v1/admin/"):
            return True
        if path == "/api/v1/worlds" and method == "POST":
            return True
        if path == "/api/v1/worlds/generate" and method == "POST":
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
        if (
            len(parts) == 7
            and parts[:4] == ["", "api", "v1", "worlds"]
            and parts[5:] == ["configuration", "apply-once"]
            and method == "POST"
        ):
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

    @app.middleware("http")
    async def authoring_connection_guard(request: Request, call_next):
        path = request.url.path.rstrip("/")
        method = request.method.upper()
        pairing_bootstrap = path == "/api/v1/auth/management" and method == "POST"
        credential = request.headers.get("X-Management-Credential", "").strip()
        authorized = pairing_bootstrap
        if credential:
            authorization = request.headers.get("Authorization", "")
            if not authorization.lower().startswith("bearer "):
                return JSONResponse(status_code=401, content={"detail": {"code": "authentication_required", "message": "需要 Bearer 登录令牌"}})
            try:
                signed_in = domain.account_from_token(authorization[7:].strip())
                manager = domain.management_account_from_token(credential)
            except DomainError as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": {"code": exc.code, "message": str(exc)}})
            if signed_in.id != manager.id:
                return JSONResponse(status_code=403, content={"detail": {"code": "management_account_mismatch", "message": "创作连接与当前登录账号不匹配"}})
            authorized = True
        elif requires_management(method, path) and not pairing_bootstrap:
            return JSONResponse(status_code=403, content={"detail": {"code": "management_connection_required", "message": "切换创作模式需要服务端管理员连接凭证"}})

        context_token = management_authorized.set(authorized)
        try:
            with domain.store.connection_scope():
                return await call_next(request)
        finally:
            management_authorized.reset(context_token)

    class Credentials(BaseModel):
        username: str = Field(min_length=2, max_length=64)
        password: str = Field(min_length=8)
        initialization_code: str | None = None
        invitation_code: str | None = None
        display_name: str | None = None

    class AccountUpdateRequest(BaseModel):
        username: str | None = Field(default=None, min_length=2, max_length=64)
        current_password: str = Field(min_length=8)
        new_password: str | None = Field(default=None, min_length=8)

    class QqUnbindRequest(BaseModel):
        current_password: str = Field(min_length=8)

    class QqBindingRequest(BaseModel):
        qq_id: str = Field(min_length=1, max_length=32)
        code: str = Field(min_length=20, max_length=64)
        session_origin: str | None = Field(default=None, max_length=256)

    class ConnectorAdminWorldRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)
        paused: bool

    class ConnectorAdminDeleteWorldRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)
        confirmed_world_name: str = Field(min_length=1, max_length=120)

    class ConnectorAdminBackupRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)

    class ConnectorAdminInvitationRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)
        hours: int = Field(default=24, ge=1, le=720)

    class ConnectorAdminInvitationRevokeRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)
        code: str = Field(min_length=1, max_length=128)

    class ConnectorAdminRollbackRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)
        node_id: str = Field(min_length=1, max_length=64)

    class ConnectorWorldSwitchRequest(BaseModel):
        sender_qq_id: str = Field(min_length=1, max_length=32)
        configured_admin_qq: str = Field(min_length=1, max_length=32)
        confirm_online_disconnect: bool = False

    class WorldSwitchRequest(BaseModel):
        confirm_online_disconnect: bool = False

    class CreateWorldRequest(BaseModel):
        name: str = Field(min_length=1, max_length=120)
        mode: WorldMode = WorldMode.STATIC
        settings: dict[str, Any] = Field(default_factory=dict)
        state: dict[str, Any] = Field(default_factory=dict)
        generation_request_id: str | None = None

    class WorldviewGenerationRequest(BaseModel):
        prompt: str = Field(min_length=1, max_length=12000)
        request_id: str = Field(min_length=1, max_length=160)
        world_id: str | None = None

    class WorldConfigurationRequest(BaseModel):
        mode: WorldMode | None = None
        settings: dict[str, Any] = Field(default_factory=dict)

    class ContentRequest(BaseModel):
        content: str = Field(min_length=1)
        request_id: str | None = None
        expected_version: int | None = None
        actual_tokens: int = Field(default=0, ge=0)

    class JoinRequest(BaseModel):
        invite_code: str | None = None
        display_name: str | None = None

    class InvitationRevokeRequest(BaseModel):
        code: str = Field(min_length=1, max_length=128)

    class UsageRequest(BaseModel):
        ledger: str
        actual_tokens: int = Field(ge=0)

    class BackupSettingsRequest(BaseModel):
        automatic_enabled: bool
        interval_hours: int = Field(default=24, ge=1, le=8760)
        retention_count: int | None = Field(default=None, ge=1)

    class BackupImportRequest(BaseModel):
        snapshot: dict[str, Any]
        account_mappings: dict[str, str | None] | None = None

    class BackupPreviewRequest(BaseModel):
        snapshot: dict[str, Any]

    def account(authorization: str | None):
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "需要 Bearer 登录令牌")
        try:
            signed_in = domain.account_from_token(authorization[7:].strip())
            if signed_in.is_admin and not management_authorized.get():
                return Account(signed_in.id, signed_in.username, False, signed_in.created_at)
            return signed_in
        except DomainError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc

    def call(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DomainError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": str(exc)}) from exc

    @app.get("/api/v1/health")
    def health():
        bridge = getattr(domain, "bot_bridge", None)
        return {
            "status": "ok",
            "service": "virtual-domain",
            "version": "0.1.0",
            "bot_mode": domain.bot_mode,
            "bot_connected": bool(bridge and bridge.connected),
        }

    @app.post("/api/v1/auth/register", status_code=201)
    def register(body: Credentials):
        acc, token, world_id = call(
            domain.register_with_credential,
            body.username,
            body.password,
            initialization_code=body.initialization_code,
            invitation_code=body.invitation_code,
            display_name=body.display_name,
        )
        return {"account": dataclass_dict(acc), "token": token, "world_id": world_id}

    @app.post("/api/v1/auth/login")
    def login(body: Credentials):
        acc, token = call(domain.login, body.username, body.password)
        return {"account": dataclass_dict(acc), "token": token}

    @app.post("/api/v1/auth/management")
    def connect_management(body: dict[str, str], authorization: str | None = Header(default=None)):
        acc = account(authorization)
        token = call(domain.issue_management_token, acc, str(body.get("pairing_code", "")))
        return {"management_token": token}

    @app.delete("/api/v1/auth/management")
    def disconnect_management(
        authorization: str | None = Header(default=None),
        x_management_credential: str | None = Header(default=None),
    ):
        acc = account(authorization)
        call(domain.revoke_management_token, acc, x_management_credential or "")
        return {"ok": True}

    @app.patch("/api/v1/auth/me")
    def update_me(body: AccountUpdateRequest, authorization: str | None = Header(default=None)):
        acc = account(authorization)
        updated = call(
            domain.update_account,
            acc,
            username=body.username,
            current_password=body.current_password,
            new_password=body.new_password,
        )
        return dataclass_dict(updated)

    @app.get("/api/v1/auth/me/qq-binding")
    def qq_binding_status(authorization: str | None = Header(default=None)):
        return domain.qq_binding_status(account(authorization))

    @app.post("/api/v1/auth/me/qq-binding-code")
    def create_qq_binding_code(authorization: str | None = Header(default=None)):
        return domain.issue_qq_binding_code(account(authorization))

    @app.delete("/api/v1/auth/me/qq-binding")
    def remove_qq_binding(body: QqUnbindRequest, authorization: str | None = Header(default=None)):
        call(domain.unbind_qq, account(authorization), body.current_password)
        return {"ok": True, "bound": False}

    @app.post("/api/v1/auth/logout")
    def logout(authorization: str | None = Header(default=None)):
        if authorization and authorization.lower().startswith("bearer "):
            domain.revoke_token(authorization[7:].strip())
        return {"ok": True}

    @app.get("/api/v1/bridge/worlds")
    def connector_worlds(x_connector_token: str | None = Header(default=None)):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return {"worlds": domain.connector_worlds()}

    @app.get("/api/v1/bridge/admin/summary")
    def connector_admin_world_summary(
        x_connector_token: str | None = Header(default=None),
        x_admin_qq: str | None = Header(default=None),
        x_configured_admin_qq: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_admin_world_summary,
            x_admin_qq or "",
            x_configured_admin_qq or "",
        )

    @app.get("/api/v1/bridge/admin/interactions")
    def connector_admin_pending_interactions(
        x_connector_token: str | None = Header(default=None),
        x_admin_qq: str | None = Header(default=None),
        x_configured_admin_qq: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_admin_pending_interactions,
            x_admin_qq or "",
            x_configured_admin_qq or "",
        )

    @app.get("/api/v1/bridge/admin/audit")
    def connector_admin_audit(
        limit: int = Query(default=10, ge=1, le=20),
        x_connector_token: str | None = Header(default=None),
        x_admin_qq: str | None = Header(default=None),
        x_configured_admin_qq: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_admin_audit_summary,
            x_admin_qq or "",
            x_configured_admin_qq or "",
            limit=limit,
        )

    @app.websocket("/api/v1/bridge/ws")
    async def connector_websocket(socket: WebSocket):
        from ..core.bot_bridge import BotBridge

        bridge = getattr(domain, "bot_bridge", None)
        if not isinstance(bridge, BotBridge):
            await socket.close(code=1013)
            return
        await bridge.accept(socket)

    @app.post("/api/v1/bridge/qq/bind")
    def connector_bind_qq(body: QqBindingRequest, x_connector_token: str | None = Header(default=None)):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        call(domain.bind_qq, body.qq_id, body.code, session_origin=body.session_origin)
        return {"ok": True}

    @app.post("/api/v1/bridge/admin/worlds/{world_id}/current")
    def connector_admin_switch_world(
        world_id: str,
        body: ConnectorWorldSwitchRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        world = call(
            domain.connector_set_current_world,
            body.sender_qq_id,
            body.configured_admin_qq,
            world_id,
            confirm_online_disconnect=body.confirm_online_disconnect,
        )
        return {"ok": True, "world_id": world.id, "status": world.status.value, "current": world.current}

    @app.post("/api/v1/bridge/admin/worlds/{world_id}/state")
    def connector_admin_world_state(
        world_id: str,
        body: ConnectorAdminWorldRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        world = call(
            domain.connector_pause_world,
            body.sender_qq_id,
            body.configured_admin_qq,
            world_id,
            body.paused,
        )
        return {"ok": True, "world_id": world.id, "status": world.status.value}

    @app.post("/api/v1/bridge/admin/worlds/{world_id}/delete")
    def connector_admin_delete_world(
        world_id: str,
        body: ConnectorAdminDeleteWorldRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_delete_world,
            body.sender_qq_id,
            body.configured_admin_qq,
            world_id,
            body.confirmed_world_name,
        )

    @app.post("/api/v1/bridge/admin/worlds/{world_id}/invitations", status_code=201)
    def connector_admin_create_invitation(
        world_id: str,
        body: ConnectorAdminInvitationRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_create_invitation,
            body.sender_qq_id,
            body.configured_admin_qq,
            world_id,
            hours=body.hours,
        )

    @app.delete("/api/v1/bridge/admin/worlds/{world_id}/invitations")
    def connector_admin_revoke_invitation(
        world_id: str,
        body: ConnectorAdminInvitationRevokeRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_revoke_invitation,
            body.sender_qq_id,
            body.configured_admin_qq,
            world_id,
            body.code,
        )

    @app.get("/api/v1/bridge/admin/worlds/{world_id}/quota")
    def connector_admin_quota(
        world_id: str,
        x_connector_token: str | None = Header(default=None),
        x_admin_qq: str | None = Header(default=None),
        x_configured_admin_qq: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(domain.connector_quota_snapshot, x_admin_qq or "", x_configured_admin_qq or "", world_id)

    @app.get("/api/v1/bridge/admin/worlds/{world_id}/nodes")
    def connector_admin_world_nodes(
        world_id: str,
        before_sequence: int | None = Query(default=None, ge=0),
        limit: int = Query(default=10, ge=1, le=20),
        x_connector_token: str | None = Header(default=None),
        x_admin_qq: str | None = Header(default=None),
        x_configured_admin_qq: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_admin_world_nodes,
            x_admin_qq or "",
            x_configured_admin_qq or "",
            world_id,
            before_sequence=before_sequence,
            limit=limit,
        )

    @app.post("/api/v1/bridge/admin/worlds/{world_id}/rollback")
    def connector_admin_rollback_world(
        world_id: str,
        body: ConnectorAdminRollbackRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(
            domain.connector_admin_rollback_world,
            body.sender_qq_id,
            body.configured_admin_qq,
            world_id,
            body.node_id,
        )

    @app.get("/api/v1/bridge/admin/backups")
    def connector_list_backups(
        sender_qq_id: str,
        configured_admin_qq: str,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return {"backups": call(domain.connector_list_backups, sender_qq_id, configured_admin_qq)}

    @app.post("/api/v1/bridge/admin/backups", status_code=201)
    def connector_create_backup(
        body: ConnectorAdminBackupRequest,
        x_connector_token: str | None = Header(default=None),
    ):
        bridge = getattr(domain, "bot_bridge", None)
        if bridge is None:
            raise HTTPException(409, detail={"code": "bot_bridge_disabled", "message": "AstrBot 接入模式未启用"})
        if not bridge.token or not secrets.compare_digest(x_connector_token or "", bridge.token):
            raise HTTPException(401, detail={"code": "invalid_connector_token", "message": "连接凭证错误"})
        return call(domain.connector_create_backup, body.sender_qq_id, body.configured_admin_qq)

    @app.get("/api/v1/worlds")
    def worlds(authorization: str | None = Header(default=None)):
        acc = account(authorization)
        return {"worlds": [domain.client_world(acc, item) for item in domain.list_worlds(acc)]}

    @app.post("/api/v1/worlds/generate")
    def generate_worldview(body: WorldviewGenerationRequest, authorization: str | None = Header(default=None)):
        return call(domain.generate_worldview, account(authorization), body.prompt, request_id=body.request_id, target_world_id=body.world_id)

    @app.post("/api/v1/worlds", status_code=201)
    def create_world(body: CreateWorldRequest, authorization: str | None = Header(default=None)):
        acc = account(authorization)
        world = call(domain.create_world, acc, body.name, mode=body.mode, settings=body.settings, state=body.state, generation_request_id=body.generation_request_id)
        return dataclass_dict(world)

    @app.delete("/api/v1/worlds/{world_id}")
    def delete_world(world_id: str, authorization: str | None = Header(default=None)):
        return call(domain.delete_world, account(authorization), world_id)

    @app.get("/api/v1/worlds/{world_id}/configuration")
    def world_configuration(world_id: str, authorization: str | None = Header(default=None)):
        return call(domain.world_configuration, account(authorization), world_id)

    @app.patch("/api/v1/worlds/{world_id}/configuration")
    def stage_world_configuration(world_id: str, body: WorldConfigurationRequest, authorization: str | None = Header(default=None)):
        return call(domain.stage_world_configuration, account(authorization), world_id, settings=body.settings, mode=body.mode)

    @app.post("/api/v1/worlds/{world_id}/configuration/apply-once")
    def apply_world_configuration_once(
        world_id: str,
        body: dict[str, str],
        authorization: str | None = Header(default=None),
    ):
        return dataclass_dict(
            call(
                domain.apply_pending_configuration_once,
                account(authorization),
                world_id,
                request_id=str(body.get("request_id", "")),
            )
        )

    @app.get("/api/v1/worlds/{world_id}/admin/interactions")
    def admin_world_interactions(world_id: str, authorization: str | None = Header(default=None)):
        return call(domain.admin_world_interactions, account(authorization), world_id)

    @app.post("/api/v1/worlds/{world_id}/current")
    def current(
        world_id: str,
        body: WorldSwitchRequest,
        authorization: str | None = Header(default=None),
    ):
        return dataclass_dict(
            call(
                domain.set_current_world,
                account(authorization),
                world_id,
                confirm_online_disconnect=body.confirm_online_disconnect,
            )
        )

    @app.post("/api/v1/worlds/{world_id}/pause")
    def pause(world_id: str, authorization: str | None = Header(default=None)):
        return dataclass_dict(call(domain.pause_world, account(authorization), world_id, True))

    @app.post("/api/v1/worlds/{world_id}/resume")
    def resume(world_id: str, authorization: str | None = Header(default=None)):
        return dataclass_dict(call(domain.pause_world, account(authorization), world_id, False))

    @app.post("/api/v1/worlds/join-by-invite")
    def join_by_invite(body: JoinRequest, authorization: str | None = Header(default=None)):
        if not body.invite_code:
            raise HTTPException(400, detail={"code": "invitation_required", "message": "请输入管理员提供的邀请码"})
        world_id = call(domain.redeem_invitation, account(authorization), body.invite_code, display_name=body.display_name)
        return {"world_id": world_id}

    @app.post("/api/v1/worlds/{world_id}/join")
    def join(world_id: str, body: JoinRequest | None = None, authorization: str | None = Header(default=None)):
        acc = account(authorization)
        if body and body.invite_code:
            call(domain.redeem_invitation, acc, body.invite_code, display_name=body.display_name, expected_world_id=world_id)
        else:
            call(domain.join_world, acc, world_id, display_name=body.display_name if body else None)
        return {"ok": True}

    @app.post("/api/v1/worlds/{world_id}/invitations")
    def invitation(world_id: str, body: dict[str, int] | None = None, authorization: str | None = Header(default=None)):
        result = call(domain.create_invitation, account(authorization), world_id, hours=(body or {}).get("hours", 24))
        return {"code": result[0], "expires_at": result[1]}

    @app.delete("/api/v1/worlds/{world_id}/invitations")
    def revoke_invitation(
        world_id: str,
        body: InvitationRevokeRequest,
        authorization: str | None = Header(default=None),
    ):
        return call(domain.revoke_invitation, account(authorization), world_id, body.code)

    @app.post("/api/v1/worlds/{world_id}/presence")
    def presence(world_id: str, body: dict[str, bool], authorization: str | None = Header(default=None)):
        return call(domain.touch_presence, account(authorization), world_id, active=body.get("active", False))

    @app.get("/api/v1/worlds/{world_id}/runtime")
    def runtime(world_id: str, authorization: str | None = Header(default=None)):
        return call(domain.runtime_status, account(authorization), world_id)

    @app.post("/api/v1/worlds/{world_id}/leave")
    def leave(world_id: str, authorization: str | None = Header(default=None)):
        call(domain.leave_world, account(authorization), world_id)
        return {"ok": True}

    @app.get("/api/v1/worlds/{world_id}")
    def world(world_id: str, authorization: str | None = Header(default=None)):
        acc = account(authorization)
        return domain.client_world(acc, call(domain.get_world, acc, world_id), management=bool(acc.is_admin and management_authorized.get()))

    @app.get("/api/v1/worlds/{world_id}/quota")
    def quota(world_id: str, authorization: str | None = Header(default=None)):
        call(domain.get_world, account(authorization), world_id)
        return domain.quota_snapshot(world_id).as_dict()

    @app.post("/api/v1/worlds/{world_id}/usage")
    def usage(world_id: str, body: UsageRequest, authorization: str | None = Header(default=None)):
        return call(domain.record_usage, account(authorization), world_id, body.ledger, body.actual_tokens).as_dict()

    @app.post("/api/v1/worlds/{world_id}/quota")
    def set_quota(world_id: str, body: dict[str, int | None], authorization: str | None = Header(default=None)):
        return call(domain.set_quota_limits, account(authorization), world_id, **body).as_dict()

    @app.post("/api/v1/worlds/{world_id}/actions")
    def action(world_id: str, body: ContentRequest, authorization: str | None = Header(default=None)):
        event = call(domain.submit_action, account(authorization), world_id, body.content, request_id=body.request_id, expected_version=body.expected_version, actual_tokens=body.actual_tokens)
        return dataclass_dict(event) if hasattr(event, "__dataclass_fields__") else event

    @app.post("/api/v1/worlds/{world_id}/chat")
    def chat(world_id: str, body: ContentRequest, authorization: str | None = Header(default=None)):
        event = call(domain.submit_chat, account(authorization), world_id, body.content, request_id=body.request_id, actual_tokens=body.actual_tokens)
        return dataclass_dict(event)

    @app.get("/api/v1/worlds/{world_id}/interactions")
    def interactions(world_id: str, authorization: str | None = Header(default=None)):
        return call(domain.interactions, account(authorization), world_id)

    @app.post("/api/v1/worlds/{world_id}/interactions/{interaction_id}/response")
    def respond_interaction(world_id: str, interaction_id: str, body: ContentRequest, authorization: str | None = Header(default=None)):
        return call(domain.respond_interaction, account(authorization), world_id, interaction_id, body.content)

    @app.get("/api/v1/worlds/{world_id}/events")
    def events(world_id: str, after: int = 0, authorization: str | None = Header(default=None)):
        return {"events": [dataclass_dict(item) for item in call(domain.events, account(authorization), world_id, after_sequence=after, management=management_authorized.get())]}

    @app.get("/api/v1/worlds/{world_id}/nodes")
    def world_nodes(world_id: str, authorization: str | None = Header(default=None)):
        return {"nodes": call(domain.world_nodes, account(authorization), world_id)}

    @app.post("/api/v1/worlds/{world_id}/rollback")
    def rollback_world(world_id: str, body: dict[str, str], authorization: str | None = Header(default=None)):
        return call(domain.rollback_world, account(authorization), world_id, str(body.get("node_id", "")))

    @app.get("/api/v1/admin/accounts")
    def admin_accounts(authorization: str | None = Header(default=None)):
        return {"accounts": call(domain.admin_accounts, account(authorization))}

    @app.get("/api/v1/admin/model-profiles")
    def get_model_profiles(authorization: str | None = Header(default=None)):
        account(authorization)
        if model_settings is None:
            raise HTTPException(503, detail={"code": "model_settings_unavailable", "message": "模型配置管理未初始化"})
        return model_settings.public_settings()

    @app.patch("/api/v1/admin/model-profiles")
    def update_model_profiles(body: dict[str, Any], authorization: str | None = Header(default=None)):
        acc = account(authorization)
        if model_settings is None:
            raise HTTPException(503, detail={"code": "model_settings_unavailable", "message": "模型配置管理未初始化"})
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
        try:
            result = model_settings.update(body)
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "invalid_model_profile", "message": str(exc)}) from exc
        call(
            domain.record_model_settings_update,
            acc,
            role,
            changed_fields=changed_fields,
            api_key_changed=api_key_changed or bool(body.get("clear_api_key")),
        )
        return result

    @app.post("/api/v1/admin/bot-mode")
    def switch_bot_mode(body: dict[str, Any], authorization: str | None = Header(default=None)):
        acc = account(authorization)
        if model_settings is None:
            raise HTTPException(503, detail={"code": "model_settings_unavailable", "message": "模型配置管理未初始化"})
        try:
            return call(
                model_settings.switch_bot_mode,
                acc,
                str(body.get("bot_mode", "")),
                confirmed=body.get("confirmed") is True,
            )
        except ValueError as exc:
            raise HTTPException(400, detail={"code": "invalid_bot_mode", "message": str(exc)}) from exc

    @app.post("/api/v1/admin/accounts/{target_account_id}/reset-password")
    def reset_account_password(target_account_id: str, body: dict[str, str], authorization: str | None = Header(default=None)):
        call(domain.reset_account_password, account(authorization), target_account_id, str(body.get("new_password", "")))
        return {"ok": True}

    @app.get("/api/v1/admin/world-generation-usage")
    def world_generation_usage(authorization: str | None = Header(default=None)):
        return call(domain.service_generation_usage, account(authorization))

    @app.get("/api/v1/admin/backups/settings")
    def backup_settings(authorization: str | None = Header(default=None)):
        return call(domain.backup_configuration, account(authorization))

    @app.post("/api/v1/admin/backups/settings")
    def set_backup_settings(body: BackupSettingsRequest, authorization: str | None = Header(default=None)):
        return call(domain.set_backup_configuration, account(authorization), automatic_enabled=body.automatic_enabled, interval_hours=body.interval_hours, retention_count=body.retention_count)

    @app.get("/api/v1/admin/backups")
    def backups(authorization: str | None = Header(default=None)):
        return {"backups": call(domain.list_backups, account(authorization))}

    @app.post("/api/v1/admin/backups")
    def create_backup(authorization: str | None = Header(default=None)):
        return call(domain.create_backup, account(authorization), kind="manual")

    @app.get("/api/v1/admin/backups/{backup_id}/download")
    def download_backup(backup_id: str, authorization: str | None = Header(default=None)):
        path, filename = call(domain.backup_download_path, account(authorization), backup_id)
        return FileResponse(path, media_type="application/json", filename=filename)

    @app.post("/api/v1/admin/backups/import/preview")
    def preview_backup_import(body: BackupPreviewRequest, authorization: str | None = Header(default=None)):
        return call(domain.preview_backup_import, account(authorization), body.snapshot)

    @app.post("/api/v1/admin/backups/import")
    def import_backup(body: dict[str, Any], authorization: str | None = Header(default=None)):
        snapshot = body.get("snapshot") if isinstance(body, dict) and "snapshot" in body else body
        mappings = body.get("account_mappings") if isinstance(body, dict) else None
        return call(domain.restore_backup, account(authorization), snapshot, account_mappings=mappings)

    @app.post("/api/v1/admin/backups/{backup_id}/restore")
    def restore_backup(backup_id: str, authorization: str | None = Header(default=None)):
        return call(domain.restore_backup, account(authorization), backup_id)

    @app.delete("/api/v1/admin/backups/{backup_id}")
    def delete_backup(backup_id: str, authorization: str | None = Header(default=None)):
        call(domain.delete_backup, account(authorization), backup_id)
        return {"ok": True}

    @app.get("/api/v1/admin/audit")
    def audit(limit: int = 100, authorization: str | None = Header(default=None)):
        return {"entries": call(domain.audit_logs, account(authorization), limit=limit)}

    return app




