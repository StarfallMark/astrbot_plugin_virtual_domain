"""虚拟域第一版核心服务。

这里实现的是可独立测试的业务规则，HTTP/WebSocket 和桌面 UI 只负责把输入
转换为这些方法的参数。配置模型时执行世界推演、Bot 回合和记忆检索，
未配置模型时保留纯事件开发模式。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import copy
import secrets
import sqlite3
import os
import threading
import uuid
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from collections.abc import Callable
from pathlib import Path

from .models import Account, Event, EventKind, ParticipantKind, QuotaSnapshot, World, WorldMode, WorldStatus
from .providers import ModelProvider, ModelProviderError
from .simulation import (
    SimulationError,
    SimulationInput,
    SimulationResult,
    WorldSimulationEngine,
    contains_dangerous_situation,
)
from .storage import SQLiteStore
from .memory import WorldMemory
from .state_schema import validate_state_values
from .world_runtime import WorldRuntime, timestamp, validate_settings, world_timezone


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _estimated_tokens(value: Any, *, output_limit: int = 0) -> int:
    """Conservative reservation estimate only; never use this to charge usage."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    ascii_chars = sum(ord(char) < 128 for char in text)
    other_chars = len(text) - ascii_chars
    prompt_estimate = (ascii_chars + 2) // 3 + other_chars
    safety_margin = max(128, (prompt_estimate + 3) // 4)
    return max(1, prompt_estimate + safety_margin + max(0, int(output_limit)))


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 210_000)
    return f"pbkdf2_sha256$210000${salt.hex()}${digest.hex()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


class DomainError(Exception):
    """可安全返回给客户端的业务异常。"""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class AuthenticationError(DomainError):
    status_code = 401
    code = "authentication_required"


class PermissionDenied(DomainError):
    status_code = 403
    code = "permission_denied"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class QuotaExceeded(DomainError):
    status_code = 429
    code = "quota_exceeded"


class ModelExecutionError(DomainError):
    status_code = 502
    code = "model_execution_failed"


class VirtualDomainService:
    def __init__(
        self,
        db_path: str = "virtual_domain.sqlite3",
        *,
        simulation_engine: WorldSimulationEngine | None = None,
        chat_provider: ModelProvider | None = None,
        bot_bridge: Any = None,
        bot_mode: str = "standalone",
        memory_configs: tuple[dict, dict] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = SQLiteStore(db_path)
        self.simulation_engine = simulation_engine
        self.chat_provider = chat_provider or (simulation_engine.provider if simulation_engine else None)
        self.bot_bridge = bot_bridge
        self.bot_mode = bot_mode
        self._usage_support_recorder: Callable[[str, bool], None] | None = None
        self._usage_support_status_reader: Callable[[], dict[str, str]] | None = None
        self._play_lock = threading.RLock()
        self._backup_lock = threading.Lock()
        self.memory = WorldMemory(self.store, *memory_configs) if memory_configs else None
        self._clock = clock or (lambda: datetime.now(UTC))
        self.runtime = WorldRuntime(self)
        self._offline_inflight: str | None = None
        self._ensure_world_node_baselines()
        self._initialization_code = self._load_or_create_initialization_code()
        self._management_pairing_code = self._load_or_create_management_pairing_code()

    def _node_runtime_snapshot_conn(self, conn: sqlite3.Connection, world_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()
        runtime = dict(row) if row is not None else {}
        runtime["player_interaction_seconds"] = {
            str(item["account_id"]): float(item["interaction_seconds"] or 0)
            for item in conn.execute(
                "SELECT account_id,interaction_seconds FROM player_activity WHERE world_id=?",
                (world_id,),
            ).fetchall()
        }
        return runtime

    def _insert_world_node_conn(
        self,
        conn: sqlite3.Connection,
        world_id: str,
        *,
        event_id: str | None,
        sequence: int,
        state_version: int,
        state_json: str,
        status: str,
        mode: str,
        settings_json: str,
        created_at: str,
        node_id: str | None = None,
        configuration_version: int | None = None,
        runtime_json: str | None = None,
    ) -> str:
        if configuration_version is None:
            row = conn.execute(
                "SELECT version FROM world_configuration_versions "
                "WHERE world_id=? AND status='active' ORDER BY version DESC LIMIT 1",
                (world_id,),
            ).fetchone()
            configuration_version = int(row["version"]) if row is not None else 1
        if runtime_json is None:
            runtime_json = self.store.encode(self._node_runtime_snapshot_conn(conn, world_id))
        node_id = node_id or str(uuid.uuid4())
        conn.execute(
            "INSERT INTO world_nodes(id,world_id,event_id,sequence,state_version,state_json,status,mode,"
            "settings_json,configuration_version,runtime_json,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                node_id,
                world_id,
                event_id,
                int(sequence),
                int(state_version),
                state_json,
                status,
                mode,
                settings_json,
                int(configuration_version),
                runtime_json,
                created_at,
            ),
        )
        return node_id

    def _ensure_world_node_baselines(self) -> None:
        """Give pre-node databases a safe rollback point at their current state."""
        with self.store.transaction() as conn:
            worlds = conn.execute("SELECT * FROM worlds ORDER BY created_at,id").fetchall()
            for world in worlds:
                if conn.execute(
                    "SELECT 1 FROM world_nodes WHERE world_id=? LIMIT 1",
                    (world["id"],),
                ).fetchone():
                    continue
                latest = conn.execute(
                    "SELECT id,sequence FROM events WHERE world_id=? ORDER BY sequence DESC LIMIT 1",
                    (world["id"],),
                ).fetchone()
                sequence = int(latest["sequence"]) if latest is not None else 0
                conn.execute(
                    "UPDATE world_runtime SET event_sequence=MAX(event_sequence,?) WHERE world_id=?",
                    (sequence, world["id"]),
                )
                self._insert_world_node_conn(
                    conn,
                    world["id"],
                    event_id=str(latest["id"]) if latest is not None else None,
                    sequence=sequence,
                    state_version=int(world["state_version"]),
                    state_json=world["state_json"],
                    status=world["status"],
                    mode=world["mode"],
                    settings_json=world["settings_json"],
                    created_at=world["updated_at"],
                )

    def close(self) -> None:
        self.store.close()

    # ----- 账号和会话 -------------------------------------------------
    def register(self, username: str, password: str) -> tuple[Account, str]:
        """Trusted in-process provisioning helper; network handlers must use credentialed registration."""

        username = username.strip()
        if len(username) < 2 or len(username) > 64:
            raise DomainError("用户名长度必须为 2 到 64 个字符", code="invalid_username")
        if len(password) < 8:
            raise DomainError("密码至少需要 8 位", code="invalid_password")
        now = utc_now()
        account_id = str(uuid.uuid4())
        with self.store.transaction() as conn:
            exists = conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
            is_admin = 1 if exists is None else 0
            try:
                conn.execute(
                    "INSERT INTO accounts(id, username, password_hash, is_admin, created_at) VALUES(?,?,?,?,?)",
                    (account_id, username, _password_hash(password), is_admin, now),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise Conflict("用户名已存在", code="username_taken") from exc
                raise
        account = Account(account_id, username, bool(is_admin), now)
        return account, self._new_session(account_id)

    @property
    def management_pairing_code(self) -> str | None:
        """Local bootstrap secret required to mint a separate authoring connection."""

        return self._management_pairing_code

    def _management_pairing_file(self) -> Path | None:
        if self.store.path == ":memory:":
            return None
        database = Path(self.store.path).resolve()
        return database.with_name(database.name + ".management-pairing-code")

    def _load_or_create_management_pairing_code(self) -> str | None:
        code = ""
        pairing_path = self._management_pairing_file()
        if pairing_path and pairing_path.is_file():
            try:
                code = pairing_path.read_text(encoding="utf-8").strip()
            except OSError:
                code = ""
        row = self.store.connection.execute(
            "SELECT value FROM service_meta WHERE key='management_pairing_hash'"
        ).fetchone()
        if not code and row is not None:
            # The server can verify the old code but cannot recover it. Keep authoring
            # unavailable until an operator rotates the local pairing file explicitly.
            return None
        if not code:
            code = secrets.token_urlsafe(32)
            if pairing_path:
                pairing_path.parent.mkdir(parents=True, exist_ok=True)
                pairing_path.write_text(code + "\n", encoding="utf-8")
                try:
                    os.chmod(pairing_path, 0o600)
                except OSError:
                    pass
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO service_meta(key,value) VALUES('management_pairing_hash',?) "
                "ON CONFLICT(key) DO NOTHING",
                (digest,),
            )
        saved = self.store.connection.execute(
            "SELECT value FROM service_meta WHERE key='management_pairing_hash'"
        ).fetchone()
        if saved is None or not hmac.compare_digest(digest, saved["value"]):
            return None
        return code

    def issue_management_token(self, account: Account, pairing_code: str) -> str:
        self._require_admin(account)
        row = self.store.connection.execute(
            "SELECT value FROM service_meta WHERE key='management_pairing_hash'"
        ).fetchone()
        provided = hashlib.sha256(pairing_code.strip().encode()).hexdigest()
        if row is None or not hmac.compare_digest(provided, row["value"]):
            raise AuthenticationError("创作模式配对凭证错误", code="invalid_management_pairing")
        token = secrets.token_urlsafe(48)
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO management_sessions(id,account_id,token_hash,created_at) VALUES(?,?,?,?)",
                (str(uuid.uuid4()), account.id, hashlib.sha256(token.encode()).hexdigest(), utc_now()),
            )
            self._audit_conn(conn, account.id, "management.connect", account.id, "success", {})
        return token

    def management_account_from_token(self, token: str) -> Account:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.store.connection.execute(
            "SELECT a.id,a.username,a.is_admin,a.created_at FROM management_sessions m "
            "JOIN accounts a ON a.id=m.account_id WHERE m.token_hash=? AND m.active=1",
            (token_hash,),
        ).fetchone()
        if row is None or not row["is_admin"]:
            raise AuthenticationError("创作模式连接无效或已撤销", code="invalid_management_session")
        return Account(row["id"], row["username"], True, row["created_at"])

    def revoke_management_token(self, account: Account, token: str) -> None:
        self._require_admin(account)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE management_sessions SET active=0 WHERE token_hash=? AND account_id=?",
                (token_hash, account.id),
            )
            self._audit_conn(conn, account.id, "management.disconnect", account.id, "success", {})

    @property
    def initialization_code(self) -> str | None:
        """返回仅供本机启动日志展示的一次性管理员初始化码。"""

        return self._initialization_code

    def _setup_code_file(self) -> Path | None:
        if self.store.path == ":memory:":
            return None
        database = Path(self.store.path).resolve()
        return database.with_name(database.name + ".admin-setup-code")

    def _load_or_create_initialization_code(self) -> str | None:
        setup_path = self._setup_code_file()
        account_exists = self.store.connection.execute("SELECT 1 FROM accounts LIMIT 1").fetchone()
        if account_exists:
            with self.store.transaction() as conn:
                conn.execute("DELETE FROM service_meta WHERE key='admin_setup_hash'")
            if setup_path:
                setup_path.unlink(missing_ok=True)
            return None

        code = ""
        if setup_path and setup_path.is_file():
            try:
                code = setup_path.read_text(encoding="utf-8").strip()
            except OSError:
                code = ""
        if not code:
            code = secrets.token_urlsafe(32)
            if setup_path:
                setup_path.parent.mkdir(parents=True, exist_ok=True)
                setup_path.write_text(code + "\n", encoding="utf-8")
                try:
                    os.chmod(setup_path, 0o600)
                except OSError:
                    pass
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO service_meta(key,value) VALUES('admin_setup_hash',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (digest,),
            )
        return code

    @staticmethod
    def _validate_registration(username: str, password: str) -> tuple[str, str]:
        username = username.strip()
        if len(username) < 2 or len(username) > 64:
            raise DomainError("用户名长度必须为 2 到 64 个字符", code="invalid_username")
        if len(password) < 8:
            raise DomainError("密码至少需要 8 位", code="invalid_password")
        return username, password

    def register_with_credential(
        self,
        username: str,
        password: str,
        *,
        initialization_code: str | None = None,
        invitation_code: str | None = None,
        display_name: str | None = None,
    ) -> tuple[Account, str, str | None]:
        """Public registration path: first admin uses the local setup code; players need an invite."""

        if bool(initialization_code) == bool(invitation_code):
            raise DomainError("管理员首次注册需要服务端初始化码；玩家注册需要管理员邀请", code="registration_credential_required")
        if initialization_code:
            account, token = self._register_initial_admin(username, password, initialization_code)
            return account, token, None
        account, token, world_id = self._register_invited_player(
            username, password, invitation_code or "", display_name=display_name
        )
        return account, token, world_id

    def _register_initial_admin(self, username: str, password: str, setup_code: str) -> tuple[Account, str]:
        username, password = self._validate_registration(username, password)
        now = utc_now()
        account_id = str(uuid.uuid4())
        provided_hash = hashlib.sha256(setup_code.strip().encode()).hexdigest()
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM accounts LIMIT 1").fetchone():
                raise Conflict("管理员已初始化", code="admin_already_initialized")
            row = conn.execute("SELECT value FROM service_meta WHERE key='admin_setup_hash'").fetchone()
            if row is None or not hmac.compare_digest(provided_hash, row["value"]):
                raise AuthenticationError("初始化码错误或已失效", code="invalid_initialization_code")
            try:
                conn.execute(
                    "INSERT INTO accounts(id,username,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
                    (account_id, username, _password_hash(password), 1, now),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise Conflict("用户名已存在", code="username_taken") from exc
                raise
            conn.execute("DELETE FROM service_meta WHERE key='admin_setup_hash'")
            self._audit_conn(conn, account_id, "admin.initialize", account_id, "success", {})
        setup_path = self._setup_code_file()
        if setup_path:
            setup_path.unlink(missing_ok=True)
        self._initialization_code = None
        account = Account(account_id, username, True, now)
        return account, self._new_session(account_id)

    def _register_invited_player(
        self, username: str, password: str, invitation_code: str, *, display_name: str | None
    ) -> tuple[Account, str, str]:
        username, password = self._validate_registration(username, password)
        now = utc_now()
        account_id = str(uuid.uuid4())
        code_hash = hashlib.sha256(invitation_code.strip().encode()).hexdigest()
        account = Account(account_id, username, False, now)
        with self._play_lock, self.store.transaction() as conn:
            invitation = conn.execute("SELECT * FROM invitations WHERE code_hash=?", (code_hash,)).fetchone()
            if invitation is None:
                raise Conflict("邀请不存在或已失效", code="invalid_invitation")
            if invitation["redeemed_by"]:
                raise Conflict("邀请已经被使用", code="invitation_redeemed")
            if invitation["expires_at"] <= utc_now():
                raise Conflict("邀请已经过期", code="invitation_expired")
            world = conn.execute("SELECT is_current,status FROM worlds WHERE id=?", (invitation["world_id"],)).fetchone()
            if not world or not world["is_current"] or world["status"] == "ended":
                raise Conflict("邀请对应的世界尚未运行或已经结束", code="world_not_current")
            try:
                conn.execute(
                    "INSERT INTO accounts(id,username,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)",
                    (account_id, username, _password_hash(password), 0, now),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise Conflict("用户名已存在", code="username_taken") from exc
                raise
            self._join_world_connection(conn, account, invitation["world_id"], display_name)
            conn.execute("UPDATE invitations SET redeemed_by=? WHERE id=?", (account_id, invitation["id"]))
            self._audit_conn(conn, account_id, "invitation.register", invitation["world_id"], "success", {})
            world_id = invitation["world_id"]
        self.runtime.touch(world_id, account_id, self._clock(), active=True)
        runtime = self.runtime.advance_clock(self.get_world(account, world_id), self._clock())
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE player_activity SET interaction_seconds=? WHERE world_id=? AND account_id=?",
                (runtime["active_seconds"], world_id, account_id),
            )
        return account, self._new_session(account_id), world_id

    def login(self, username: str, password: str) -> tuple[Account, str]:
        row = self.store.connection.execute(
            "SELECT id, username, password_hash, is_admin, created_at FROM accounts WHERE username = ?",
            (username.strip(),),
        ).fetchone()
        if row is None or not _password_matches(password, row["password_hash"]):
            raise AuthenticationError("用户名或密码错误", code="invalid_credentials")
        return Account(row["id"], row["username"], bool(row["is_admin"]), row["created_at"]), self._new_session(row["id"])

    def update_account(
        self,
        account: Account,
        *,
        username: str | None = None,
        current_password: str | None = None,
        new_password: str | None = None,
    ) -> Account:
        """更新当前账号的用户名或密码。

        账号已登录并不等于可以无条件修改凭据，因此两类修改都要求再次
        输入当前密码。密码只保存哈希，审计日志也不会记录任何密码内容。
        """

        self._require_authenticated(account)
        if username is None and new_password is None:
            raise DomainError("至少提供新的用户名或新的密码", code="empty_account_update")
        if not current_password:
            raise AuthenticationError("修改账号信息需要当前密码", code="current_password_required")
        username = username.strip() if username is not None else None
        if username is not None and (len(username) < 2 or len(username) > 64):
            raise DomainError("用户名长度必须为 2 到 64 个字符", code="invalid_username")
        if new_password is not None and len(new_password) < 8:
            raise DomainError("密码至少需要 8 位", code="invalid_password")

        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT username, password_hash, is_admin, created_at FROM accounts WHERE id=?",
                (account.id,),
            ).fetchone()
            if row is None:
                raise AuthenticationError("账号不存在", code="account_not_found")
            if not _password_matches(current_password, row["password_hash"]):
                raise AuthenticationError("当前密码错误", code="invalid_current_password")

            next_username = username if username is not None else row["username"]
            changed: list[str] = []
            try:
                if username is not None and username != row["username"]:
                    conn.execute("UPDATE accounts SET username=? WHERE id=?", (username, account.id))
                    changed.append("username")
                if new_password is not None:
                    conn.execute("UPDATE accounts SET password_hash=? WHERE id=?", (_password_hash(new_password), account.id))
                    changed.append("password")
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise Conflict("用户名已存在", code="username_taken") from exc
                raise
            self._audit_conn(conn, account.id, "account.update", account.id, "success", {"changed": changed})
            return Account(account.id, next_username, bool(row["is_admin"]), row["created_at"])

    def qq_binding_status(self, account: Account) -> dict[str, Any]:
        """Return the QQ binding owned by the authenticated account."""

        self._require_authenticated(account)
        row = self.store.connection.execute(
            "SELECT qq_id FROM qq_bindings WHERE account_id=?", (account.id,)
        ).fetchone()
        return {"bound": row is not None, "qq_id": row["qq_id"] if row is not None else None}

    def qq_conversation_origin(self, account_id: str) -> str | None:
        """Return the explicitly captured daily-chat route for a bound account."""
        row = self.store.connection.execute(
            "SELECT session_origin FROM qq_bindings WHERE account_id=?", (str(account_id),)
        ).fetchone()
        origin = str(row["session_origin"] or "").strip() if row else ""
        return origin or None

    def issue_qq_binding_code(self, account: Account) -> dict[str, str]:
        """Issue a single-use, five-minute QQ binding code without storing it in plaintext."""

        self._require_authenticated(account)
        now_dt = self._clock()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        now_dt = now_dt.astimezone(UTC).replace(microsecond=0)
        now = now_dt.isoformat()
        expires_at = (now_dt + timedelta(minutes=5)).isoformat()
        for _ in range(5):
            code = secrets.token_urlsafe(24)
            code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
            with self.store.transaction() as conn:
                if conn.execute("SELECT 1 FROM qq_binding_codes WHERE code_hash=?", (code_hash,)).fetchone():
                    continue
                conn.execute("DELETE FROM qq_binding_codes WHERE account_id=?", (account.id,))
                conn.execute(
                    "INSERT INTO qq_binding_codes(code_hash,account_id,created_at,expires_at) VALUES(?,?,?,?)",
                    (code_hash, account.id, now, expires_at),
                )
                self._audit_conn(conn, account.id, "account.qq_binding_code", account.id, "success", {})
                return {"code": code, "expires_at": expires_at}
        raise Conflict("暂时无法生成绑定码，请稍后重试", code="qq_binding_code_unavailable")

    def bind_qq(self, qq_id: str, code: str, *, session_origin: str | None = None) -> None:
        """Redeem a desktop-issued code for the QQ sender verified by the connector."""

        qq_id = str(qq_id).strip()
        code = str(code).strip()
        if not qq_id or not qq_id.isdecimal() or len(qq_id) > 32:
            raise DomainError("QQ 号格式无效", code="invalid_qq_id")
        normalized_origin = str(session_origin or "").strip()
        if len(normalized_origin) > 256 or any(ord(char) < 32 for char in normalized_origin):
            raise DomainError("QQ 会话来源格式无效", code="invalid_qq_session_origin")
        if len(code) < 20 or len(code) > 64:
            raise AuthenticationError("绑定码无效或已过期", code="invalid_qq_binding_code")
        now_dt = self._clock()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=UTC)
        now_dt = now_dt.astimezone(UTC).replace(microsecond=0)
        now = now_dt.isoformat()
        code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT account_id,expires_at,redeemed_at FROM qq_binding_codes WHERE code_hash=?",
                (code_hash,),
            ).fetchone()
            if row is None or row["redeemed_at"] is not None:
                raise AuthenticationError("绑定码无效或已使用", code="invalid_qq_binding_code")
            expiry = datetime.fromisoformat(row["expires_at"])
            if expiry <= now_dt:
                conn.execute("DELETE FROM qq_binding_codes WHERE code_hash=?", (code_hash,))
                raise AuthenticationError("绑定码已过期，请在客户端重新生成", code="expired_qq_binding_code")
            existing = conn.execute(
                "SELECT qq_id FROM qq_bindings WHERE account_id=?", (row["account_id"],)
            ).fetchone()
            if existing is not None and existing["qq_id"] != qq_id:
                raise Conflict("该账号已绑定其他 QQ，请先在客户端解除绑定", code="qq_account_already_bound")
            owner = conn.execute(
                "SELECT account_id FROM qq_bindings WHERE qq_id=?", (qq_id,)
            ).fetchone()
            if owner is not None and owner["account_id"] != row["account_id"]:
                raise Conflict("该 QQ 已绑定其他虚拟域账号", code="qq_already_bound")
            if existing is None:
                conn.execute(
                    "INSERT INTO qq_bindings(account_id,qq_id,created_at,session_origin) VALUES(?,?,?,?)",
                    (row["account_id"], qq_id, now, normalized_origin),
                )
            elif normalized_origin:
                conn.execute(
                    "UPDATE qq_bindings SET session_origin=? WHERE account_id=?",
                    (normalized_origin, row["account_id"]),
                )
            conn.execute("UPDATE qq_binding_codes SET redeemed_at=? WHERE code_hash=?", (now, code_hash))
            self._audit_conn(conn, row["account_id"], "account.qq_bind", row["account_id"], "success", {"platform": "qq"})

    def unbind_qq(self, account: Account, current_password: str) -> None:
        """Remove only the authenticated account's QQ association."""

        self._require_authenticated(account)
        if not current_password:
            raise AuthenticationError("解除 QQ 绑定需要当前密码", code="current_password_required")
        with self.store.transaction() as conn:
            row = conn.execute(
                "SELECT password_hash FROM accounts WHERE id=?", (account.id,)
            ).fetchone()
            if row is None:
                raise AuthenticationError("账号不存在", code="account_not_found")
            if not _password_matches(current_password, row["password_hash"]):
                raise AuthenticationError("当前密码错误", code="invalid_current_password")
            deleted = conn.execute("DELETE FROM qq_bindings WHERE account_id=?", (account.id,))
            if deleted.rowcount == 0:
                raise Conflict("当前账号尚未绑定 QQ", code="qq_not_bound")
            conn.execute("DELETE FROM qq_binding_codes WHERE account_id=?", (account.id,))
            self._audit_conn(conn, account.id, "account.qq_unbind", account.id, "success", {"platform": "qq"})

    def connector_admin_account(self, sender_qq_id: str, configured_admin_qq: str) -> Account:
        """Authenticate QQ management against both plugin allowlist and bound admin account."""

        sender_qq_id = str(sender_qq_id).strip()
        configured_admin_qq = str(configured_admin_qq).strip()
        if (
            not sender_qq_id.isdecimal()
            or len(sender_qq_id) > 32
            or not configured_admin_qq.isdecimal()
            or len(configured_admin_qq) > 32
            or not hmac.compare_digest(sender_qq_id, configured_admin_qq)
        ):
            raise PermissionDenied("此 QQ 不是插件配置的虚拟域管理员", code="qq_admin_not_allowed")
        row = self.store.connection.execute(
            "SELECT a.id,a.username,a.is_admin,a.created_at FROM qq_bindings q "
            "JOIN accounts a ON a.id=q.account_id WHERE q.qq_id=?",
            (sender_qq_id,),
        ).fetchone()
        if row is None or not bool(row["is_admin"]):
            raise PermissionDenied("该 QQ 未绑定管理员账号", code="qq_admin_not_bound")
        return Account(row["id"], row["username"], True, row["created_at"])

    def connector_pause_world(
        self, sender_qq_id: str, configured_admin_qq: str, world_id: str, paused: bool
    ) -> World:
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        return self.pause_world(account, world_id, paused)

    def connector_delete_world(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        world_id: str,
        confirmed_world_name: str,
    ) -> dict[str, Any]:
        """Delete a world only after the bound QQ administrator confirms its exact name."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        world = self.get_world(account, world_id)
        if confirmed_world_name != world.name:
            raise Conflict(
                "世界名称确认不匹配；请先查看世界列表，再原样确认要删除的世界名称。",
                code="world_name_confirmation_mismatch",
            )
        return self.delete_world(account, world_id)

    def connector_create_invitation(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        world_id: str,
        *,
        hours: int = 24,
    ) -> dict[str, str]:
        """Create a one-time invitation for the verified QQ administrator."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        code, expires_at = self.create_invitation(account, world_id, hours=hours)
        return {"world_id": world_id, "code": code, "expires_at": expires_at}

    def connector_revoke_invitation(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        world_id: str,
        code: str,
    ) -> dict[str, str]:
        """Revoke one unused invitation for the verified QQ administrator."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        return self.revoke_invitation(account, world_id, code)

    def connector_create_backup(self, sender_qq_id: str, configured_admin_qq: str) -> dict[str, Any]:
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        return self.create_backup(account, kind="manual")

    def connector_list_backups(self, sender_qq_id: str, configured_admin_qq: str) -> list[dict[str, Any]]:
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        return self.list_backups(account)

    def connector_quota_snapshot(
        self, sender_qq_id: str, configured_admin_qq: str, world_id: str
    ) -> dict[str, Any]:
        """Return a world's token ledgers only to its verified QQ administrator."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        self.get_world(account, world_id)
        return self.quota_snapshot(world_id).as_dict() | {
            "usage_support": self.model_usage_support_status()
        }

    def connector_admin_world_nodes(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        world_id: str,
        *,
        before_sequence: int | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return a bounded node page to the verified QQ administrator."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        world = self.get_world(account, world_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise DomainError("每页节点数量必须在 1 到 20 之间", code="invalid_node_limit")
        nodes = self.world_nodes(
            account,
            world_id,
            before_sequence=before_sequence,
            limit=limit,
        )
        next_before_sequence = (
            nodes[-1]["sequence"]
            if len(nodes) == limit and nodes[-1]["sequence"] > 0
            else None
        )
        return {
            "world": {
                "id": world.id,
                "name": world.name,
                "status": world.status.value,
                "mode": world.mode.value,
                "state_version": world.state_version,
            },
            "nodes": nodes,
            "next_before_sequence": next_before_sequence,
        }

    def connector_admin_rollback_world(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        world_id: str,
        node_id: str,
    ) -> dict[str, Any]:
        """Roll back through the authenticated AstrBot administrator bridge."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        return self.rollback_world(account, world_id, node_id)

    def connector_admin_world_summary(
        self, sender_qq_id: str, configured_admin_qq: str
    ) -> dict[str, Any]:
        """Return a concise, non-narrative status snapshot to the verified QQ admin."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        worlds = self.list_worlds(account)
        current = next((world for world in worlds if world.current), None)
        if current is None:
            return {"current_world": None, "world_count": len(worlds)}

        runtime = self.runtime_status(account, current.id)
        interaction_rows = self.store.connection.execute(
            "SELECT status,COUNT(*) AS count FROM pending_interactions "
            "WHERE world_id=? GROUP BY status",
            (current.id,),
        ).fetchall()
        interaction_counts = {str(row["status"]): int(row["count"]) for row in interaction_rows}
        return {
            "current_world": {
                "id": current.id,
                "name": current.name,
                "mode": current.mode.value,
                "status": current.status.value,
                "state_version": current.state_version,
            },
            "world_count": len(worlds),
            "players_online": int(runtime["players_online"]),
            "bot_request_running": bool(runtime["bot_request_running"]),
            "bot_daily": runtime["bot_daily"],
            "interactions": interaction_counts,
            "quota": self.quota_snapshot(current.id).as_dict(),
        }

    def connector_admin_pending_interactions(
        self, sender_qq_id: str, configured_admin_qq: str
    ) -> dict[str, Any]:
        """List actionable or failed interaction details for the verified QQ admin."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        current = next((world for world in self.list_worlds(account) if world.current), None)
        if current is None:
            return {"current_world": None, "interactions": []}
        return self.admin_world_interactions(account, current.id)

    def connector_admin_audit_summary(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        *,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Return a bounded, detail-free audit view for the verified QQ administrator."""
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise DomainError("审计记录条数必须在 1 到 20 之间", code="invalid_audit_limit")
        entries = self.audit_logs(account, limit=limit)
        return {
            "entries": [
                {
                    "id": str(item["id"]),
                    "action": str(item["action"]),
                    "target": str(item["target"]),
                    "result": str(item["result"]),
                    "created_at": str(item["created_at"]),
                }
                for item in entries
            ]
        }

    def admin_world_interactions(self, account: Account, world_id: str) -> dict[str, Any]:
        """List pending, resolving, or failed interactions through the authoring connection."""
        self._require_admin(account)
        current = self.get_world(account, world_id)
        rows = self.store.connection.execute(
            "SELECT id,request_id,actor_kind,target_name,action,prompt,deadline_at,status,"
            "response_text,response_source,resolution_attempts,created_at "
            "FROM pending_interactions WHERE world_id=? AND status IN ('pending','ready','failed') "
            "ORDER BY CASE status WHEN 'failed' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END,"
            "created_at,id LIMIT 10",
            (current.id,),
        ).fetchall()
        now = self._clock()
        items = []
        for row in rows:
            deadline = datetime.fromisoformat(row["deadline_at"])
            items.append({
                "id": row["id"],
                "request_id": row["request_id"],
                "actor_kind": row["actor_kind"],
                "target_name": row["target_name"],
                "action": row["action"],
                "prompt": row["prompt"],
                "deadline_at": row["deadline_at"],
                "seconds_remaining": max(0, int((deadline - now).total_seconds())),
                "status": row["status"],
                "response_text": row["response_text"],
                "response_source": row["response_source"],
                "resolution_attempts": int(row["resolution_attempts"]),
                "created_at": row["created_at"],
            })
        return {
            "current_world": {"id": current.id, "name": current.name},
            "interactions": items,
        }

    def admin_accounts(self, account: Account) -> list[dict[str, Any]]:
        self._require_admin(account)
        rows = self.store.connection.execute(
            "SELECT id,username,is_admin,created_at FROM accounts ORDER BY created_at,username"
        ).fetchall()
        return [
            {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def reset_account_password(self, account: Account, target_account_id: str, new_password: str) -> None:
        self._require_admin(account)
        if target_account_id == account.id:
            raise DomainError("不能通过此功能重置自己的密码，请使用账号设置", code="cannot_reset_self")
        if len(new_password) < 8:
            raise DomainError("密码至少需要 8 位", code="invalid_password")
        with self.store.transaction() as conn:
            target = conn.execute("SELECT id FROM accounts WHERE id=?", (target_account_id,)).fetchone()
            if target is None:
                raise NotFound("账号不存在", code="account_not_found")
            conn.execute("UPDATE accounts SET password_hash=? WHERE id=?", (_password_hash(new_password), target_account_id))
            conn.execute("UPDATE sessions SET active=0 WHERE account_id=?", (target_account_id,))
            conn.execute("UPDATE management_sessions SET active=0 WHERE account_id=?", (target_account_id,))
            self._audit_conn(conn, account.id, "account.password.reset", target_account_id, "success", {})

    def _new_session(self, account_id: str) -> str:
        raw = secrets.token_urlsafe(36)
        now = utc_now()
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(id, account_id, token_hash, created_at) VALUES(?,?,?,?)",
                (str(uuid.uuid4()), account_id, token_hash, now),
            )
        return raw

    def account_from_token(self, token: str) -> Account:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        row = self.store.connection.execute(
            "SELECT a.id, a.username, a.is_admin, a.created_at FROM sessions s JOIN accounts a ON a.id=s.account_id "
            "WHERE s.token_hash=? AND s.active=1",
            (token_hash,),
        ).fetchone()
        if row is None:
            raise AuthenticationError("登录已失效", code="invalid_session")
        return Account(row["id"], row["username"], bool(row["is_admin"]), row["created_at"])

    def revoke_token(self, token: str) -> None:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.store.transaction() as conn:
            conn.execute("UPDATE sessions SET active=0 WHERE token_hash=?", (token_hash,))

    # ----- 世界 -------------------------------------------------------
    def generate_worldview(
        self, account: Account, prompt: str, *, request_id: str, target_world_id: str | None = None
    ) -> dict[str, Any]:
        """Generate an editable world draft through the world model, never the Bot player pipeline."""

        self._require_admin(account)
        prompt = prompt.strip()
        request_id = request_id.strip()
        if not prompt or len(prompt) > 12000:
            raise DomainError("世界生成提示词长度必须为 1 至 12000 个字符", code="invalid_worldview_prompt")
        if not request_id or len(request_id) > 160:
            raise DomainError("世界生成请求缺少有效 request_id", code="invalid_request_id")
        if self.simulation_engine is None:
            raise ModelExecutionError("尚未配置世界推演模型，无法生成世界观", code="world_model_unavailable")
        self._require_admin(account)
        if target_world_id:
            self.get_world(account, target_world_id)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        now = utc_now()
        with self.store.transaction() as conn:
            previous = conn.execute(
                "SELECT account_id,prompt_hash,target_world_id,status,result_json,error_code FROM worldview_generation_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if previous:
                if (previous["account_id"] != account.id or not hmac.compare_digest(previous["prompt_hash"], prompt_hash)
                    or previous["target_world_id"] != target_world_id):
                    raise Conflict("request_id 已用于另一条世界生成请求", code="request_id_conflict")
                if previous["status"] == "succeeded" and previous["result_json"]:
                    return self.store.decode(previous["result_json"])
                if previous["status"] == "pending":
                    raise Conflict("相同的世界生成请求仍在处理中", code="request_in_progress")
                raise Conflict("该世界生成请求已失败；如需重试请创建新的请求", code=previous["error_code"] or "previous_generation_failed")
            conn.execute(
                "INSERT INTO worldview_generation_requests(request_id,account_id,prompt_hash,target_world_id,status,created_at,updated_at) VALUES(?,?,?,?, 'pending',?,?)",
                (request_id, account.id, prompt_hash, target_world_id, now, now),
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "你为虚拟域游戏生成可游玩的世界草案。你不是玩家，不要模拟 Bot 行动，也不要替玩家选择角色或行动。"
                    "只返回 JSON，不要 Markdown。浅层设定写角色可以通过常识、公开教育或亲身可见信息知道的事实；"
                    "深层设定只放少量非公开、非日常、会影响后续推演的事实或谜团，不要重复浅层设定。"
                    "初始状态只记录开局已经确定的结构化事实，不预设玩家尚未做出的行为。开场叙事负责描述可观察的初始场景。"
                    'JSON 格式：{"name":"世界名","shallow_worldview":"公开常识","deep_worldview":"少量深层事实","opening_narrative":"玩家进入后可见的开场","initial_state":{}}。'
                    "name 最多 60 字；初始状态必须是 JSON 对象；内容应与用户要求一致，避免擅自加入强制题材。"
                ),
            },
            {"role": "user", "content": prompt},
        ]
        usage_total = 0
        try:
            provider = self.simulation_engine.provider
            worldview_max_tokens = getattr(provider.config, "worldview_max_output_tokens", None)
            if worldview_max_tokens is None:
                worldview_max_tokens = 3072
            last_error: Exception | None = None
            generated: dict[str, Any] | None = None
            for attempt in range(2):
                self._ensure_model_request_allowed_for_account(account, target_world_id=target_world_id)
                try:
                    if target_world_id:
                        response = self._complete_model_call(
                            account,
                            target_world_id,
                            "world",
                            f"{request_id}:attempt:{attempt + 1}",
                            provider,
                            messages,
                            max_tokens=worldview_max_tokens,
                        )
                    else:
                        response = provider.complete(
                            messages,
                            max_tokens=worldview_max_tokens,
                            temperature=0.7,
                        )
                except ModelProviderError as exc:
                    last_error = exc
                    if not exc.retryable or attempt >= 1:
                        break
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({"role": "user", "content": "请严格按指定 JSON 格式重试生成。"})
                    continue
                if response.total_tokens is not None:
                    used = int(response.total_tokens)
                    if used > 0 and not target_world_id:
                        self._record_service_generation_usage(account, used, target_world_id=target_world_id)
                    usage_total += used
                if not response.usage_supported:
                    raise ModelExecutionError(
                        "上游没有返回实际 Token 用量，本次世界生成失败且未计入额度；请检查模型供应商用量支持。",
                        code="usage_unavailable",
                    )
                try:
                    raw = response.text.strip()
                    if raw.startswith("```"):
                        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                        if raw.endswith("```"):
                            raw = raw[:-3].rstrip()
                    value = json.loads(raw)
                    if not isinstance(value, dict):
                        raise ValueError("模型输出必须是 JSON 对象")
                    name = str(value.get("name", "")).strip()
                    shallow = str(value.get("shallow_worldview", "")).strip()
                    deep = str(value.get("deep_worldview", "")).strip()
                    opening = str(value.get("opening_narrative", "")).strip()
                    initial_state = value.get("initial_state", {})
                    if not name or len(name) > 60 or not shallow or not opening or not isinstance(initial_state, dict):
                        raise ValueError("模型未返回完整世界名、浅层设定、开场叙事或合法初始状态")
                    generated = {"name": name, "shallow_worldview": shallow, "deep_worldview": deep, "opening_narrative": opening, "initial_state": initial_state, "actual_tokens": usage_total}
                    break
                except (json.JSONDecodeError, TypeError, ValueError) as exc:
                    last_error = exc
                    messages.extend([
                        {"role": "assistant", "content": response.text[:12000]},
                        {"role": "user", "content": "上一条不是有效完整结果。请重新只返回符合指定 schema 的 JSON 对象。"},
                    ])
            if generated is None:
                raise ModelExecutionError(f"世界观生成失败：{last_error or '模型没有返回有效结果'}", code="worldview_generation_failed")
            saved = dict(generated)
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE worldview_generation_requests SET status='succeeded',actual_tokens=?,result_json=?,updated_at=? WHERE request_id=? AND account_id=?",
                    (usage_total, self.store.encode(saved), utc_now(), request_id, account.id),
                )
                self._audit_conn(conn, account.id, "worldview.generate", request_id, "success", {"actual_tokens": usage_total, "prompt_hash": prompt_hash})
            return generated
        except DomainError as exc:
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE worldview_generation_requests SET status='failed',error_code=?,updated_at=? WHERE request_id=? AND account_id=? AND status='pending'",
                    (exc.code, utc_now(), request_id, account.id),
                )
                self._audit_conn(conn, account.id, "worldview.generate", request_id, "failed", {"actual_tokens": usage_total, "error_code": exc.code})
            raise
        except Exception as exc:
            with self.store.transaction() as conn:
                conn.execute(
                    "UPDATE worldview_generation_requests SET status='failed',error_code='worldview_generation_failed',updated_at=? WHERE request_id=? AND account_id=? AND status='pending'",
                    (utc_now(), request_id, account.id),
                )
                self._audit_conn(conn, account.id, "worldview.generate", request_id, "failed", {"actual_tokens": usage_total, "error": type(exc).__name__})
            raise ModelExecutionError(f"世界观生成失败：{exc}", code="worldview_generation_failed") from exc

    def _ensure_model_request_allowed_for_account(self, account: Account, *, target_world_id: str | None = None) -> None:
        self._require_admin(account)
        if self.simulation_engine is None:
            raise ModelExecutionError("尚未配置世界推演模型", code="world_model_unavailable")
        if target_world_id:
            self.get_world(account, target_world_id)
            if self.quota_snapshot(target_world_id).exhausted():
                raise QuotaExceeded("目标世界额度已耗尽，不能生成世界观", code="quota_exceeded")

    def _record_service_generation_usage(
        self, account: Account, actual_tokens: int, *, target_world_id: str | None = None
    ) -> None:
        if target_world_id:
            self.record_usage(account, target_world_id, "world", actual_tokens)
            return
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT INTO service_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+CAST(excluded.value AS INTEGER)",
                ("unassigned_world_model_tokens", str(actual_tokens)),
            )

    def service_generation_usage(self, account: Account) -> dict[str, int]:
        self._require_admin(account)
        row = self.store.connection.execute(
            "SELECT value FROM service_meta WHERE key='unassigned_world_model_tokens'"
        ).fetchone()
        return {"unassigned_world_model_tokens": int(row["value"]) if row else 0}

    def create_world(
        self,
        account: Account,
        name: str,
        *,
        mode: WorldMode = WorldMode.STATIC,
        settings: dict[str, Any] | None = None,
        state: dict[str, Any] | None = None,
        generation_request_id: str | None = None,
    ) -> World:
        self._require_admin(account)
        name = name.strip()
        if not name:
            raise DomainError("世界名称不能为空", code="invalid_world_name")
        world_settings = dict(settings or {})
        world_settings.setdefault("bot_name", "Bot")
        world_settings.setdefault("role_mode", "self")
        world_settings.setdefault("interaction_timeout_seconds", 300)
        opening_narrative = str(world_settings.pop("opening_narrative", "")).strip()
        try:
            validate_settings(world_settings)
            validate_state_values(
                world_settings.get("state_schema", {}),
                state or {},
                allow_reserved=False,
            )
        except ValueError as exc:
            raise DomainError(str(exc), code="invalid_settings") from exc
        now = utc_now()
        world = World(str(uuid.uuid4()), name, WorldMode(mode), WorldStatus.ACTIVE, 0, False, world_settings, state or {}, now, now)
        with self.store.transaction() as conn:
            generation = None
            if generation_request_id:
                generation = conn.execute(
                    "SELECT account_id,target_world_id,billed_world_id,status,actual_tokens FROM worldview_generation_requests WHERE request_id=?",
                    (generation_request_id,),
                ).fetchone()
                if (generation is None or generation["account_id"] != account.id or generation["status"] != "succeeded"
                    or generation["target_world_id"] is not None or generation["billed_world_id"] is not None):
                    raise Conflict("世界生成草案不存在、尚未成功或已经结算", code="invalid_generation_draft")
            is_current = conn.execute("SELECT 1 FROM worlds WHERE is_current=1").fetchone() is None
            world.current = is_current
            conn.execute(
                "INSERT INTO worlds(id,name,mode,status,state_version,is_current,settings_json,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (world.id, world.name, world.mode.value, world.status.value, 0, int(is_current), self.store.encode(world.settings), self.store.encode(world.state), now, now),
            )
            conn.execute("INSERT INTO quota_limits(world_id) VALUES(?)", (world.id,))
            conn.execute("INSERT INTO world_runtime(world_id,anchor_at) VALUES(?,?)", (world.id, timestamp(self._clock())))
            if generation is not None:
                generation_tokens = int(generation["actual_tokens"] or 0)
                conn.execute("UPDATE quota_limits SET world_used=world_used+? WHERE world_id=?", (generation_tokens, world.id))
                conn.execute(
                    "UPDATE service_meta SET value=CAST(MAX(0,CAST(value AS INTEGER)-?) AS TEXT) WHERE key='unassigned_world_model_tokens'",
                    (generation_tokens,),
                )
                conn.execute(
                    "UPDATE worldview_generation_requests SET billed_world_id=? WHERE request_id=? AND billed_world_id IS NULL",
                    (world.id, generation_request_id),
                )
            if opening_narrative:
                conn.execute(
                    "INSERT INTO events(id,world_id,sequence,kind,actor_id,content,state_version,token_usage,request_id,created_at) VALUES(?,?,1,?,?,?,0,0,'world-opening',?)",
                    (str(uuid.uuid4()), world.id, EventKind.SYSTEM.value, account.id, opening_narrative, now),
                )
            conn.execute(
                "INSERT INTO world_configuration_versions(world_id,version,mode,settings_json,status,created_by,created_at,applied_at) VALUES(?,1,?,?, 'active',?,?,?)",
                (world.id, world.mode.value, self.store.encode(world.settings), account.id, now, now),
            )
            opening = conn.execute(
                "SELECT id,sequence FROM events WHERE world_id=? ORDER BY sequence DESC LIMIT 1",
                (world.id,),
            ).fetchone()
            opening_sequence = int(opening["sequence"]) if opening is not None else 0
            conn.execute(
                "UPDATE world_runtime SET event_sequence=? WHERE world_id=?",
                (opening_sequence, world.id),
            )
            self._insert_world_node_conn(
                conn,
                world.id,
                event_id=str(opening["id"]) if opening is not None else None,
                sequence=opening_sequence,
                state_version=world.state_version,
                state_json=self.store.encode(world.state),
                status=world.status.value,
                mode=world.mode.value,
                settings_json=self.store.encode(world.settings),
                created_at=now,
                configuration_version=1,
            )
            self._audit_conn(conn, account.id, "world.create", world.id, "success", {"name": world.name, "mode": world.mode.value})
        return world

    def _ensure_configuration_history(self, account: Account, world: World) -> None:
        existing = self.store.connection.execute(
            "SELECT 1 FROM world_configuration_versions WHERE world_id=? LIMIT 1", (world.id,)
        ).fetchone()
        if existing:
            return
        now = utc_now()
        with self.store.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO world_configuration_versions(world_id,version,mode,settings_json,status,created_by,created_at,applied_at) VALUES(?,1,?,?, 'active',?,?,?)",
                (world.id, world.mode.value, self.store.encode(world.settings), account.id, world.created_at or now, world.updated_at or now),
            )

    def world_configuration(self, account: Account, world_id: str) -> dict[str, Any]:
        self._require_admin(account)
        world = self.get_world(account, world_id)
        self._ensure_configuration_history(account, world)
        rows = self.store.connection.execute(
            "SELECT version,mode,settings_json,status,created_by,created_at,applied_at "
            "FROM world_configuration_versions WHERE world_id=? AND status IN ('active','pending') ORDER BY version",
            (world_id,),
        ).fetchall()
        configs = [
            {"version": int(row["version"]), "mode": row["mode"], "settings": self.store.decode(row["settings_json"]), "status": row["status"], "created_by": row["created_by"], "created_at": row["created_at"], "applied_at": row["applied_at"]}
            for row in rows
        ]
        active = next((item for item in reversed(configs) if item["status"] == "active"), None)
        pending = next((item for item in reversed(configs) if item["status"] == "pending"), None)
        return {"world_id": world_id, "active": active, "pending": pending}

    def stage_world_configuration(
        self, account: Account, world_id: str, *, settings: dict[str, Any], mode: WorldMode | str | None = None
    ) -> dict[str, Any]:
        with self._play_lock:
            return self._stage_world_configuration_locked(
                account,
                world_id,
                settings=settings,
                mode=mode,
            )

    def _stage_world_configuration_locked(
        self, account: Account, world_id: str, *, settings: dict[str, Any], mode: WorldMode | str | None = None
    ) -> dict[str, Any]:
        self._require_admin(account)
        if not isinstance(settings, dict):
            raise DomainError("世界配置必须是对象", code="invalid_settings")
        world = self.get_world(account, world_id)
        self._ensure_configuration_history(account, world)
        now = utc_now()
        with self.store.transaction() as conn:
            current_pending = conn.execute(
                "SELECT version,mode,settings_json FROM world_configuration_versions WHERE world_id=? AND status='pending' ORDER BY version DESC LIMIT 1",
                (world_id,),
            ).fetchone()
            active = conn.execute(
                "SELECT version FROM world_configuration_versions WHERE world_id=? AND status='active' ORDER BY version DESC LIMIT 1",
                (world_id,),
            ).fetchone()
            base_settings = self.store.decode(current_pending["settings_json"]) if current_pending else dict(world.settings)
            merged = dict(base_settings)
            merged.update(settings)
            immutable_identity = ("role_mode", "player_character_name", "bot_character_name")
            for key in immutable_identity:
                before = base_settings.get(key, "self" if key == "role_mode" else "")
                after = merged.get(key, "self" if key == "role_mode" else "")
                if before != after:
                    raise Conflict("世界开局后不能切换参演模式或更换绑定角色", code="role_assignment_locked")
            try:
                validate_settings(merged)
                validate_state_values(
                    merged.get("state_schema", {}),
                    world.state,
                    allow_reserved=True,
                )
                base_mode = WorldMode(current_pending["mode"]) if current_pending else world.mode
                next_mode = WorldMode(mode) if mode is not None else base_mode
            except (ValueError, TypeError) as exc:
                raise DomainError(str(exc), code="invalid_settings") from exc
            conn.execute(
                "UPDATE world_configuration_versions SET status='superseded' WHERE world_id=? AND status='pending'",
                (world_id,),
            )
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM world_configuration_versions WHERE world_id=?", (world_id,)
            ).fetchone()[0])
            conn.execute(
                "INSERT INTO world_configuration_versions(world_id,version,mode,settings_json,status,created_by,created_at,applied_at) VALUES(?,?,?,?, 'pending',?,?,NULL)",
                (world_id, version, next_mode.value, self.store.encode(merged), account.id, now),
            )
            self._audit_conn(conn, account.id, "world.configuration.stage", world_id, "success", {"from_version": int(active["version"]) if active else None, "superseded_version": int(current_pending["version"]) if current_pending else None, "pending_version": version, "changed_keys": sorted(settings.keys()), "mode_changed": next_mode.value != world.mode.value})
        return {"world_id": world_id, "version": version, "mode": next_mode.value, "status": "pending", "will_apply_on": "next_world_simulation"}

    def _apply_pending_world_configuration(self, world_id: str) -> bool:
        now = timestamp(self._clock())
        with self.store.transaction() as conn:
            pending = conn.execute(
                "SELECT version,mode,settings_json,created_by FROM world_configuration_versions "
                "WHERE world_id=? AND status='pending' ORDER BY version DESC LIMIT 1",
                (world_id,),
            ).fetchone()
            if pending is None:
                return False
            active = conn.execute(
                "SELECT version FROM world_configuration_versions WHERE world_id=? AND status='active' ORDER BY version DESC LIMIT 1",
                (world_id,),
            ).fetchone()
            settings_json = pending["settings_json"]
            conn.execute("UPDATE world_configuration_versions SET status='superseded' WHERE world_id=? AND status='active'", (world_id,))
            conn.execute(
                "UPDATE world_configuration_versions SET status='active',applied_at=? WHERE world_id=? AND version=?",
                (now, world_id, pending["version"]),
            )
            conn.execute(
                "UPDATE worlds SET mode=?,settings_json=?,updated_at=? WHERE id=?",
                (pending["mode"], settings_json, now, world_id),
            )
            self._audit_conn(conn, pending["created_by"], "world.configuration.apply", world_id, "success", {"from_version": int(active["version"]) if active else None, "version": int(pending["version"])})
        return True

    def _restore_failed_immediate_configuration(
        self,
        account: Account,
        world_id: str,
        applied_version: int,
        previous_version: int | None,
    ) -> None:
        with self.store.transaction() as conn:
            applied = conn.execute(
                "SELECT version,status FROM world_configuration_versions "
                "WHERE world_id=? AND version=?",
                (world_id, applied_version),
            ).fetchone()
            if applied is None or applied["status"] != "active":
                return
            previous = (
                conn.execute(
                    "SELECT version,mode,settings_json FROM world_configuration_versions "
                    "WHERE world_id=? AND version=?",
                    (world_id, previous_version),
                ).fetchone()
                if previous_version is not None
                else None
            )
            if previous is None:
                return
            newer_pending = conn.execute(
                "SELECT 1 FROM world_configuration_versions "
                "WHERE world_id=? AND status='pending' AND version>? LIMIT 1",
                (world_id, applied_version),
            ).fetchone()
            failed_status = "superseded" if newer_pending else "pending"
            conn.execute(
                "UPDATE world_configuration_versions SET status=?,applied_at=NULL "
                "WHERE world_id=? AND version=?",
                (failed_status, world_id, applied_version),
            )
            conn.execute(
                "UPDATE world_configuration_versions SET status='active',applied_at=? "
                "WHERE world_id=? AND version=?",
                (utc_now(), world_id, previous["version"]),
            )
            conn.execute(
                "UPDATE worlds SET mode=?,settings_json=?,updated_at=? WHERE id=?",
                (previous["mode"], previous["settings_json"], utc_now(), world_id),
            )
            self._audit_conn(
                conn,
                account.id,
                "world.configuration.apply_once",
                world_id,
                "failed",
                {
                    "failed_version": applied_version,
                    "restored_version": int(previous["version"]),
                    "newer_pending_preserved": bool(newer_pending),
                },
            )

    def apply_pending_configuration_once(
        self,
        account: Account,
        world_id: str,
        *,
        request_id: str,
    ) -> Event:
        """Apply the staged configuration in one bounded, non-player world simulation."""
        self._require_admin(account)
        request_id = str(request_id).strip()
        if not request_id.startswith("admin-once:") or len(request_id) > 160:
            raise DomainError(
                "一次性推演请求 ID 无效",
                code="invalid_request_id",
            )
        if self.simulation_engine is None:
            raise ModelExecutionError("尚未配置世界推演模型", code="world_model_unconfigured")

        with self._play_lock:
            duplicate = self.store.connection.execute(
                "SELECT * FROM events WHERE world_id=? AND request_id=?",
                (world_id, request_id),
            ).fetchone()
            if duplicate:
                return self._event_from_row(duplicate)
            world = self.get_world(account, world_id)
            if not world.current or world.status != WorldStatus.ACTIVE:
                raise Conflict("只有当前运行中的世界可以立即推演", code="world_unavailable")
            configuration = self.world_configuration(account, world_id)
            pending = configuration.get("pending")
            if pending is None:
                raise Conflict("当前没有待生效配置", code="no_pending_configuration")
            applied_version = int(pending["version"])
            active = configuration.get("active")
            previous_version = int(active["version"]) if active else None
            instruction = (
                "管理员已明确授权这一次立即推演，用于让待生效的世界配置生效。"
                "请只推演周围环境或 NPC 的自然变化，不替任何玩家行动、作决定或改变玩家状态；"
                "若没有合适变化，只描写符合新配置的当前场景，不要制造重大事件。"
            )
            try:
                result = self._simulate_action(
                    account,
                    world_id,
                    instruction,
                    request_id=request_id,
                    expected_version=world.state_version,
                    trigger="admin_once",
                    allow_pending_interactions=False,
                )
                if not isinstance(result, Event):
                    raise ModelExecutionError(
                        "一次性推演未能提交世界结果，待生效配置仍保留",
                        code="admin_simulation_not_committed",
                    )
            except Exception:
                self._restore_failed_immediate_configuration(
                    account,
                    world_id,
                    applied_version,
                    previous_version,
                )
                raise

            with self.store.transaction() as conn:
                self._audit_conn(
                    conn,
                    account.id,
                    "world.configuration.apply_once",
                    world_id,
                    "success",
                    {
                        "applied_version": applied_version,
                        "event_id": result.id,
                        "request_id": request_id,
                    },
                )
            return result

    def list_worlds(self, account: Account) -> list[World]:
        self._require_authenticated(account)
        if account.is_admin:
            rows = self.store.connection.execute("SELECT * FROM worlds ORDER BY created_at DESC").fetchall()
        else:
            rows = self.store.connection.execute(
                "SELECT w.* FROM worlds w JOIN participants p ON p.world_id=w.id "
                "WHERE p.account_id=? ORDER BY w.created_at DESC",
                (account.id,),
            ).fetchall()
        return [self._world_from_row(row) for row in rows]

    def delete_world(self, account: Account, world_id: str) -> dict[str, Any]:
        """Permanently delete one world and its world-scoped records."""
        self._require_admin(account)
        with self._backup_lock, self._play_lock:
            row = self.store.connection.execute(
                "SELECT id,name,status,is_current FROM worlds WHERE id=?",
                (world_id,),
            ).fetchone()
            if row is None:
                raise NotFound("世界不存在", code="world_not_found")
            online_players = [
                str(player["display_name"])
                for player in self.runtime.presence(world_id, self._clock())
                if player["online"]
            ]
            with self.store.transaction() as conn:
                deleted = conn.execute("DELETE FROM worlds WHERE id=?", (world_id,))
                if deleted.rowcount != 1:
                    raise NotFound("世界不存在", code="world_not_found")
                self._audit_conn(
                    conn,
                    account.id,
                    "world.delete",
                    world_id,
                    "success",
                    {
                        "name": str(row["name"]),
                        "status": str(row["status"]),
                        "was_current": bool(row["is_current"]),
                        "online_players_disconnected": online_players,
                    },
                )
            return {
                "world_id": world_id,
                "name": str(row["name"]),
                "was_current": bool(row["is_current"]),
                "online_players_disconnected": online_players,
            }

    def connector_worlds(self) -> list[dict[str, Any]]:
        """Return non-sensitive world summaries to the authenticated AstrBot connector."""
        rows = self.store.connection.execute(
            "SELECT id,name,mode,status,is_current FROM worlds ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "mode": row["mode"],
                "status": row["status"],
                "current": bool(row["is_current"]),
            }
            for row in rows
        ]

    def get_world(self, account: Account, world_id: str) -> World:
        self._require_authenticated(account)
        row = self.store.connection.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
        if row is None:
            raise NotFound("世界不存在", code="world_not_found")
        if not account.is_admin and not self.store.connection.execute(
            "SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)
        ).fetchone():
            raise PermissionDenied("账号尚未获准进入该世界", code="world_access_denied")
        return self._world_from_row(row)

    def client_world(self, account: Account, world: World, *, management: bool = False) -> dict[str, Any]:
        from .models import dataclass_dict
        data = dataclass_dict(world)
        if not (account.is_admin and management):
            # Account login alone is a player connection; private world data requires an authoring connection.
            data["settings"] = {key: value for key, value in world.settings.items() if key in {"shallow_worldview", "role_mode", "bot_name", "player_character_name", "bot_character_name"}}
            data["state"] = {}
        return data

    def set_current_world(
        self,
        account: Account,
        world_id: str,
        *,
        confirm_online_disconnect: bool = False,
    ) -> World:
        self._require_admin(account)
        if not isinstance(confirm_online_disconnect, bool):
            raise DomainError("确认状态必须是布尔值", code="invalid_world_switch_confirmation")
        with self._play_lock:
            now = self._clock()
            worlds = self.list_worlds(account)
            current = next((world for world in worlds if world.current), None)
            target = next((world for world in worlds if world.id == world_id), None)
            if target is None:
                raise NotFound("世界不存在", code="world_not_found")
            if current is not None and current.id != world_id:
                online_players = [player for player in self.runtime.presence(current.id, now) if player["online"]]
                if online_players and not confirm_online_disconnect:
                    names = "、".join(str(player["display_name"]) for player in online_players)
                    raise Conflict(
                        f"切换确认：当前世界有 {len(online_players)} 名玩家在线（{names}）。再次确认后将断开玩家并通知世界已切换。",
                        code="world_switch_requires_confirmation",
                    )
                self.runtime.advance_clock(current, now)
                if online_players:
                    self._append_event(
                        account,
                        current.id,
                        EventKind.SYSTEM,
                        f"管理员已确认切换世界；当前在线玩家将断开连接。新世界：{target.name}。",
                        f"world-switch:{uuid.uuid4()}",
                        None,
                        0,
                        "world",
                        visible_to=["*"],
                    )
            if target.current or (current is not None and current.id == world_id):
                return target
            self.runtime.advance_clock(target, now)
            return self._set_current_world(account, world_id)

    def connector_set_current_world(
        self,
        sender_qq_id: str,
        configured_admin_qq: str,
        world_id: str,
        *,
        confirm_online_disconnect: bool = False,
    ) -> World:
        account = self.connector_admin_account(sender_qq_id, configured_admin_qq)
        return self.set_current_world(
            account,
            world_id,
            confirm_online_disconnect=confirm_online_disconnect,
        )

    def _set_current_world(self, account: Account, world_id: str) -> World:
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone() is None:
                raise NotFound("世界不存在", code="world_not_found")
            conn.execute("UPDATE world_runtime SET generation_epoch=generation_epoch+1 WHERE world_id IN (SELECT id FROM worlds WHERE is_current=1 OR id=?)", (world_id,))
            conn.execute("UPDATE worlds SET is_current=0")
            conn.execute("UPDATE participants SET online=0 WHERE world_id!=?", (world_id,))
            conn.execute("UPDATE player_activity SET disconnected=1 WHERE world_id!=?", (world_id,))
            conn.execute("UPDATE worlds SET is_current=1, updated_at=? WHERE id=?", (utc_now(), world_id))
            self._audit_conn(conn, account.id, "world.set_current", world_id, "success", {})
        return self.get_world(account, world_id)

    def pause_world(self, account: Account, world_id: str, paused: bool) -> World:
        self._require_admin(account)
        self.runtime.advance_clock(self.get_world(account, world_id), self._clock())
        return self._pause_world(account, world_id, paused)

    def _pause_world(self, account: Account, world_id: str, paused: bool) -> World:
        status = WorldStatus.PAUSED if paused else WorldStatus.ACTIVE
        with self.store.transaction() as conn:
            conn.execute("UPDATE world_runtime SET generation_epoch=generation_epoch+1 WHERE world_id=?", (world_id,))
            result = conn.execute("UPDATE worlds SET status=?, updated_at=? WHERE id=? AND status != ?", (status.value, utc_now(), world_id, WorldStatus.ENDED.value))
            if result.rowcount == 0:
                raise NotFound("世界不存在或已经结束", code="world_not_found")
            self._audit_conn(conn, account.id, "world.pause" if paused else "world.resume", world_id, "success", {})
        return self.get_world(account, world_id)

    def _world_from_row(self, row: Any) -> World:
        return World(
            row["id"], row["name"], WorldMode(row["mode"]), WorldStatus(row["status"]), int(row["state_version"]),
            bool(row["is_current"]), self.store.decode(row["settings_json"]), self.store.decode(row["state_json"]), row["created_at"], row["updated_at"],
        )

    # ----- 参与者和事件 -----------------------------------------------
    def join_world(self, account: Account, world_id: str, *, display_name: str | None = None) -> None:
        with self._play_lock:
            return self._join_world(account, world_id, display_name=display_name)

    def _join_world(self, account: Account, world_id: str, *, display_name: str | None = None) -> None:
        self._require_authenticated(account)
        existing = self.store.connection.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone()
        if not account.is_admin and not existing:
            raise PermissionDenied("加入世界需要管理员邀请", code="invitation_required")
        world = self.get_world(account, world_id)
        if world.status == WorldStatus.ENDED:
            # Reopening an ended world is read-only: do not restore presence,
            # advance time, or invoke either model.
            return
        if not world.current:
            raise Conflict("该世界未运行，请管理员先切换当前世界", code="world_not_current")
        if world.status == WorldStatus.PAUSED:
            raise Conflict("世界已暂停，请管理员先恢复", code="world_paused")
        with self.store.transaction() as conn:
            self._join_world_connection(conn, account, world_id, display_name)
        if existing and world.mode == WorldMode.FLOWING and self.simulation_engine and not self.quota_snapshot(world_id).exhausted():
            runtime = self.runtime.advance_clock(world, self._clock())
            if runtime["active_seconds"] - runtime["settled_seconds"] >= 1:
                self._simulate_action(account, world_id, "玩家重新进入世界。概括尚未结算期间的世界变化，回顾玩家角色与 Bot 在离开期间的日常；不提出新的玩家行动。", request_id=f"return:{uuid.uuid4()}", expected_version=world.state_version, trigger="return")
        self.runtime.touch(world_id, account.id, self._clock(), active=True)
        runtime = self.runtime.advance_clock(self.get_world(account, world_id), self._clock())
        with self.store.transaction() as conn:
            conn.execute("UPDATE player_activity SET interaction_seconds=? WHERE world_id=? AND account_id=?", (runtime["active_seconds"], world_id, account.id))

    def create_invitation(self, account: Account, world_id: str, *, hours: int = 24) -> tuple[str, str]:
        """创建一张单次邀请，返回明文邀请码和过期时间。"""

        self._require_admin(account)
        if hours < 1 or hours > 24 * 30:
            raise DomainError("邀请有效期必须在 1 小时到 30 天之间", code="invalid_invitation_expiry")
        self.get_world(account, world_id)
        occupied = self.store.connection.execute(
            "SELECT 1 FROM participants WHERE world_id=? AND kind='human' LIMIT 1", (world_id,)
        ).fetchone()
        if occupied:
            raise Conflict("该世界的人类玩家名额已占用", code="human_slot_occupied")
        now = datetime.now(UTC)
        expires = (now + timedelta(hours=hours)).replace(microsecond=0).isoformat()
        code = "vd_" + secrets.token_urlsafe(24)
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        with self.store.transaction() as conn:
            active = conn.execute("SELECT 1 FROM invitations WHERE world_id=? AND redeemed_by IS NULL AND expires_at>?", (world_id, utc_now())).fetchone()
            if active:
                raise Conflict("该世界已经有一张有效邀请", code="invitation_exists")
            conn.execute("INSERT INTO invitations(id,world_id,code_hash,expires_at,created_at) VALUES(?,?,?,?,?)", (str(uuid.uuid4()), world_id, code_hash, expires, utc_now()))
            self._audit_conn(conn, account.id, "invitation.create", world_id, "success", {"expires_at": expires})
        return code, expires

    def revoke_invitation(self, account: Account, world_id: str, code: str) -> dict[str, str]:
        """Revoke an unused invitation without recording its secret code."""
        self._require_admin(account)
        self.get_world(account, world_id)
        code = str(code).strip()
        if not code or len(code) > 128:
            raise DomainError("请输入有效的邀请码", code="invalid_invitation_code")
        code_hash = hashlib.sha256(code.encode()).hexdigest()
        with self.store.transaction() as conn:
            invitation = conn.execute(
                "SELECT id,redeemed_by,expires_at FROM invitations WHERE world_id=? AND code_hash=?",
                (world_id, code_hash),
            ).fetchone()
            if invitation is None:
                raise NotFound("该世界没有这张邀请码", code="invitation_not_found")
            if invitation["redeemed_by"] is not None:
                raise Conflict("邀请码已经使用，不能撤销", code="invitation_redeemed")
            deleted = conn.execute(
                "DELETE FROM invitations WHERE id=? AND redeemed_by IS NULL",
                (invitation["id"],),
            )
            if deleted.rowcount != 1:
                raise Conflict("邀请码状态已改变，请刷新后重试", code="invitation_state_changed")
            self._audit_conn(
                conn,
                account.id,
                "invitation.revoke",
                str(invitation["id"]),
                "success",
                {"world_id": world_id, "expires_at": str(invitation["expires_at"])},
            )
        return {"world_id": world_id, "invitation_id": str(invitation["id"]), "status": "revoked"}

    def redeem_invitation(self, account: Account, code: str, *, display_name: str | None = None, expected_world_id: str | None = None) -> str:
        self._require_authenticated(account)
        code_hash = hashlib.sha256(code.strip().encode()).hexdigest()
        with self.store.transaction() as conn:
            row = conn.execute("SELECT * FROM invitations WHERE code_hash=?", (code_hash,)).fetchone()
            if row is None:
                raise Conflict("邀请不存在或已失效", code="invalid_invitation")
            if row["redeemed_by"]:
                raise Conflict("邀请已经被使用", code="invitation_redeemed")
            if row["expires_at"] <= utc_now():
                raise Conflict("邀请已经过期", code="invitation_expired")
            if expected_world_id and row["world_id"] != expected_world_id:
                raise Conflict("邀请不属于这个世界", code="invitation_world_mismatch")
            world = conn.execute("SELECT is_current,status FROM worlds WHERE id=?", (row["world_id"],)).fetchone()
            if not world or not world["is_current"] or world["status"] == "ended":
                raise Conflict("邀请对应的世界尚未运行或已经结束", code="world_not_current")
            self._join_world_connection(conn, account, row["world_id"], display_name)
            conn.execute("UPDATE invitations SET redeemed_by=? WHERE id=?", (account.id, row["id"]))
            world_id = row["world_id"]
        self.runtime.touch(world_id, account.id, self._clock(), active=True)
        runtime = self.runtime.advance_clock(self.get_world(account, world_id), self._clock())
        with self.store.transaction() as conn:
            conn.execute("UPDATE player_activity SET interaction_seconds=? WHERE world_id=? AND account_id=?", (runtime["active_seconds"], world_id, account.id))
        return world_id

    def _join_world_connection(self, conn: Any, account: Account, world_id: str, display_name: str | None) -> None:
        occupied = conn.execute("SELECT 1 FROM participants WHERE world_id=? AND kind='human'", (world_id,)).fetchone()
        current = conn.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone()
        if occupied is not None and current is None:
            raise Conflict("第一版每个世界只能有一名人类玩家", code="human_slot_occupied")
        world_row = conn.execute("SELECT settings_json FROM worlds WHERE id=?", (world_id,)).fetchone()
        settings = self.store.decode(world_row["settings_json"]) if world_row else {}
        role_mode = str(settings.get("role_mode", "self"))
        if role_mode == "character":
            assigned_name = str(settings["player_character_name"]).strip()
            character_persona = str(settings["player_character_persona"]).strip()
        else:
            assigned_name = (display_name or account.username).strip() or account.username
            character_persona = str(settings.get("player_persona", ""))
        persona = {
            "role_mode": role_mode,
            "player_persona": str(settings.get("player_persona", "")),
            "character_name": assigned_name,
            "character_persona": character_persona,
        }
        conn.execute(
            "INSERT OR IGNORE INTO participants(world_id,account_id,kind,display_name,persona_json,online,last_seen) VALUES(?,?, 'human', ?, ?, 1, ?)",
            (world_id, account.id, assigned_name, self.store.encode(persona), utc_now()),
        )
        conn.execute("UPDATE participants SET online=1,last_seen=? WHERE world_id=? AND account_id=?", (utc_now(), world_id, account.id))

    def leave_world(self, account: Account, world_id: str) -> None:
        self._require_authenticated(account)
        with self.store.transaction() as conn:
            conn.execute("UPDATE participants SET online=0,last_seen=? WHERE world_id=? AND account_id=?", (utc_now(), world_id, account.id))
            conn.execute("UPDATE player_activity SET disconnected=1 WHERE world_id=? AND account_id=?", (world_id, account.id))

    def touch_presence(self, account: Account, world_id: str, *, active: bool = False) -> dict:
        self._require_authenticated(account)
        world = self.get_world(account, world_id)
        if not world.current:
            raise Conflict("世界已切换", code="world_switched")
        if not self.store.connection.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone():
            raise PermissionDenied("账号尚未加入该世界", code="not_a_participant")
        if not isinstance(active, bool):
            raise DomainError("active 必须是开关值", code="invalid_presence")
        now = self._clock()
        self.runtime.touch(world_id, account.id, now, active=active)
        players = self.runtime.presence(world_id, now)
        own = next(player for player in players if player["account_id"] == account.id)
        return {"online": own["online"], "idle_seconds": own["idle_seconds"], "offline_after_seconds": 300}

    def runtime_status(self, account: Account, world_id: str) -> dict:
        self.get_world(account, world_id)
        players = self.runtime.presence(world_id, self._clock())
        row = self.store.connection.execute("SELECT bot_day,bot_questions,bot_actions,next_bot_at FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()
        own = next((player for player in players if player["account_id"] == account.id), None)
        result = {
            "online": bool(own and own["online"]),
            "idle_seconds": own["idle_seconds"] if own else 0,
            "bot_daily": dict(row) if row else {},
            "players_online": sum(player["online"] for player in players),
            "bot_request_running": self._offline_inflight == world_id,
        }
        if account.is_admin:
            result["usage_support"] = self.model_usage_support_status()
        return result

    def model_usage_support_status(self) -> dict[str, str]:
        reader = self._usage_support_status_reader
        if reader:
            try:
                return reader()
            except Exception:
                return {}
        return {role: "unknown" for role in ("world", "bot", "embedding", "rerank")}

    def _record_model_usage_support(self, role: str, supported: bool) -> None:
        recorder = self._usage_support_recorder
        if recorder:
            try:
                recorder(role, supported)
            except Exception:
                import logging

                logging.getLogger("virtual_domain").exception(
                    "Could not persist model usage capability for role %s",
                    role,
                )

    def set_quota_limits(self, account: Account, world_id: str, **limits: int | None) -> QuotaSnapshot:
        self._require_admin(account)
        self.runtime.advance_clock(self.get_world(account, world_id), self._clock())
        allowed = {"total_limit", "bot_limit", "world_limit", "memory_limit"}
        if set(limits) - allowed:
            raise DomainError("存在未知额度字段", code="invalid_quota")
        if any(value is not None and (not isinstance(value, int) or value < 0) for value in limits.values()):
            raise DomainError("额度上限必须是非负整数或留空", code="invalid_quota")
        values = {key: limits.get(key) for key in allowed}
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone() is None:
                raise NotFound("世界不存在", code="world_not_found")
            conn.execute(
                "UPDATE quota_limits SET total_limit=?,bot_limit=?,world_limit=?,memory_limit=? WHERE world_id=?",
                (values["total_limit"], values["bot_limit"], values["world_limit"], values["memory_limit"], world_id),
            )
            self._audit_conn(conn, account.id, "quota.update", world_id, "success", values)
        return self.quota_snapshot(world_id)

    def quota_snapshot(self, world_id: str) -> QuotaSnapshot:
        active_after = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0).isoformat()
        row = self.store.connection.execute(
            """SELECT q.*,
                      COALESCE(SUM(CASE WHEN r.ledger='bot' THEN r.estimated_tokens ELSE 0 END),0) AS reserved_bot,
                      COALESCE(SUM(CASE WHEN r.ledger='world' THEN r.estimated_tokens ELSE 0 END),0) AS reserved_world,
                      COALESCE(SUM(CASE WHEN r.ledger='memory' THEN r.estimated_tokens ELSE 0 END),0) AS reserved_memory
               FROM quota_limits q
               LEFT JOIN quota_reservations r ON r.world_id=q.world_id AND r.created_at>=?
               WHERE q.world_id=?
               GROUP BY q.world_id""",
            (active_after, world_id),
        ).fetchone()
        if row is None:
            raise NotFound("额度账本不存在", code="quota_not_found")
        return QuotaSnapshot(
            bot_used=row["bot_used"],
            world_used=row["world_used"],
            memory_used=row["memory_used"],
            bot_limit=row["bot_limit"],
            world_limit=row["world_limit"],
            memory_limit=row["memory_limit"],
            total_limit=row["total_limit"],
            reserved_bot=int(row["reserved_bot"]),
            reserved_world=int(row["reserved_world"]),
            reserved_memory=int(row["reserved_memory"]),
        )

    def _reserve_quota(
        self,
        account: Account,
        world_id: str,
        ledger: str,
        estimated_tokens: int,
        request_id: str,
    ) -> str:
        """Atomically reserve shared and per-ledger quota before an upstream call."""
        self._require_authenticated(account)
        if ledger not in {"bot", "world", "memory"}:
            raise DomainError("未知额度账本", code="invalid_ledger")
        if isinstance(estimated_tokens, bool) or not isinstance(estimated_tokens, int) or estimated_tokens <= 0:
            raise DomainError("额度预留量必须是正整数", code="invalid_token_reservation")
        reservation_id = str(uuid.uuid4())
        expired_before = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0).isoformat()
        with self.store.transaction() as conn:
            quota_row = conn.execute("SELECT * FROM quota_limits WHERE world_id=?", (world_id,)).fetchone()
            if quota_row is None:
                raise NotFound("额度账本不存在", code="quota_not_found")
            # A process crash must not pin quota forever. Normal calls release or settle
            # immediately; the lease only reclaims abandoned reservations after two hours.
            conn.execute(
                "DELETE FROM quota_reservations WHERE world_id=? AND created_at<?",
                (world_id, expired_before),
            )
            reserved_rows = conn.execute(
                "SELECT ledger,SUM(estimated_tokens) AS tokens FROM quota_reservations WHERE world_id=? GROUP BY ledger",
                (world_id,),
            ).fetchall()
            reserved = {item["ledger"]: int(item["tokens"] or 0) for item in reserved_rows}
            quota = QuotaSnapshot(
                bot_used=quota_row["bot_used"],
                world_used=quota_row["world_used"],
                memory_used=quota_row["memory_used"],
                bot_limit=quota_row["bot_limit"],
                world_limit=quota_row["world_limit"],
                memory_limit=quota_row["memory_limit"],
                total_limit=quota_row["total_limit"],
                reserved_bot=reserved.get("bot", 0),
                reserved_world=reserved.get("world", 0),
                reserved_memory=reserved.get("memory", 0),
            )
            if quota.exhausted():
                raise QuotaExceeded("额度已耗尽，世界已暂停请求", code="quota_exhausted")
            available = quota.remaining(ledger)
            if available is not None and estimated_tokens > available:
                shortfall = estimated_tokens - available
                raise QuotaExceeded(
                    f"额度预留不足：本次预计需要 {estimated_tokens} Token，当前可用 {available} Token，短缺 {shortfall} Token。",
                    code="quota_reservation_insufficient",
                )
            conn.execute(
                "INSERT INTO quota_reservations(id,world_id,account_id,ledger,estimated_tokens,session_id,request_id,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (reservation_id, world_id, account.id, ledger, estimated_tokens, reservation_id, request_id, utc_now()),
            )
        return reservation_id

    def _settle_quota_reservation(
        self,
        account: Account,
        reservation_id: str,
        world_id: str,
        ledger: str,
        actual_tokens: int,
    ) -> None:
        """Release a reservation and atomically charge the upstream-reported usage once."""
        self._require_authenticated(account)
        if ledger not in {"bot", "world", "memory"}:
            raise DomainError("未知额度账本", code="invalid_ledger")
        if isinstance(actual_tokens, bool) or not isinstance(actual_tokens, int) or actual_tokens < 0:
            raise DomainError("实际 Token 用量必须是非负整数", code="invalid_token_usage")
        column = {"bot": "bot_used", "world": "world_used", "memory": "memory_used"}[ledger]
        with self.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM quota_reservation_settlements WHERE reservation_id=?",
                (reservation_id,),
            ).fetchone():
                return
            quota = conn.execute("SELECT 1 FROM quota_limits WHERE world_id=?", (world_id,)).fetchone()
            if quota is None:
                raise NotFound("额度账本不存在", code="quota_not_found")
            active = conn.execute(
                "SELECT world_id,account_id,ledger FROM quota_reservations WHERE id=?",
                (reservation_id,),
            ).fetchone()
            if active and (
                active["world_id"] != world_id
                or active["account_id"] != account.id
                or active["ledger"] != ledger
            ):
                raise DomainError("额度预留与结算请求不匹配", code="invalid_quota_reservation")
            conn.execute(
                "INSERT INTO quota_reservation_settlements(reservation_id,world_id,ledger,actual_tokens,settled_at) "
                "VALUES(?,?,?,?,?)",
                (reservation_id, world_id, ledger, actual_tokens, utc_now()),
            )
            conn.execute(
                f"UPDATE quota_limits SET {column}={column}+? WHERE world_id=?",
                (actual_tokens, world_id),
            )
            conn.execute("DELETE FROM quota_reservations WHERE id=?", (reservation_id,))

    def _release_quota_reservation(self, reservation_id: str) -> None:
        with self.store.transaction() as conn:
            conn.execute("DELETE FROM quota_reservations WHERE id=?", (reservation_id,))

    def _reserve_model_call(
        self,
        account: Account,
        world_id: str,
        ledger: str,
        request_id: str,
        messages: Any,
        max_tokens: int,
    ) -> str:
        return self._reserve_quota(
            account,
            world_id,
            ledger,
            _estimated_tokens(messages, output_limit=max_tokens),
            request_id,
        )

    def _complete_model_call(
        self,
        account: Account,
        world_id: str,
        ledger: str,
        request_id: str,
        provider: Any,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float = 0.7,
    ) -> Any:
        reservation_id = self._reserve_model_call(
            account,
            world_id,
            ledger,
            request_id,
            messages,
            max_tokens,
        )
        try:
            response = provider.complete(messages, max_tokens=max_tokens, temperature=temperature)
        except BaseException:
            self._release_quota_reservation(reservation_id)
            raise
        actual_tokens = response.total_tokens
        if actual_tokens is None or actual_tokens <= 0:
            self._release_quota_reservation(reservation_id)
            raise ModelExecutionError(
                "上游没有返回正数的实际 Token 用量，本次调用未计入额度；请检查供应商用量支持。",
                code="usage_unavailable",
            )
        self._settle_quota_reservation(account, reservation_id, world_id, ledger, actual_tokens)
        return response

    def record_usage(self, account: Account, world_id: str, ledger: str, actual_tokens: int) -> QuotaSnapshot:
        """记录已完成的模型调用用量。

        调用发生后必须入账，即使本次用量触及额度上限；因此这里不做“额度
        未耗尽”的前置拒绝。调用前的暂停由下一次请求检查 `exhausted()`。
        """

        self._require_authenticated(account)
        if ledger not in {"bot", "world", "memory"}:
            raise DomainError("未知额度账本", code="invalid_ledger")
        if isinstance(actual_tokens, bool) or not isinstance(actual_tokens, int) or actual_tokens < 0:
            raise DomainError("实际 Token 用量必须是非负整数", code="invalid_token_usage")
        column = {"bot": "bot_used", "world": "world_used", "memory": "memory_used"}[ledger]
        self.runtime.advance_clock(self.get_world(account, world_id), self._clock())
        with self.store.transaction() as conn:
            if conn.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone() is None:
                raise NotFound("世界不存在", code="world_not_found")
            conn.execute(f"UPDATE quota_limits SET {column}={column}+? WHERE world_id=?", (actual_tokens, world_id))
        return self.quota_snapshot(world_id)

    def submit_action(
        self,
        account: Account,
        world_id: str,
        content: str,
        *,
        request_id: str | None = None,
        expected_version: int | None = None,
        actual_tokens: int = 0,
    ) -> Event | dict[str, Any]:
        if self.simulation_engine is not None:
            request_id = request_id or str(uuid.uuid4())
            with self._play_lock:
                previous = self._preflight(account, world_id, content, request_id, expected_version)
                if previous:
                    return previous
                self.runtime.touch(world_id, account.id, self._clock(), active=True)
                event = self._simulate_action(account, world_id, content, request_id=request_id, expected_version=expected_version)
                if isinstance(event, Event):
                    world = self.get_world(account, world_id)
                    if world.status == WorldStatus.ACTIVE and world.settings.get("bot_turn_enabled", False) and not self.quota_snapshot(world_id).exhausted():
                        self._bot_turn(account, world_id, event.request_id + ":bot")
                return event
        return self._append_event(account, world_id, EventKind.ACTION, content, request_id, expected_version, actual_tokens, "world", visible_to=[account.id])

    def submit_chat(
        self,
        account: Account,
        world_id: str,
        content: str,
        *,
        request_id: str | None = None,
        actual_tokens: int = 0,
    ) -> Event:
        if self.chat_provider is not None or self.bot_mode == "astrbot":
            with self._play_lock:
                previous = self._preflight(account, world_id, content, request_id, None)
                if previous:
                    return previous
                self.runtime.touch(world_id, account.id, self._clock(), active=True)
                return self._generate_chat(account, world_id, content, request_id=request_id, expected_version=None)
        return self._append_event(account, world_id, EventKind.CHAT, content, request_id, None, actual_tokens, "bot", visible_to=[account.id, "bot"])

    def _ensure_model_request_allowed(self, world_id: str) -> None:
        if self.quota_snapshot(world_id).exhausted():
            raise QuotaExceeded("额度已耗尽，世界已暂停请求", code="quota_exhausted")

    def _preflight(self, account: Account, world_id: str, content: str, request_id: str | None, expected_version: int | None) -> Event | dict[str, Any] | None:
        self._require_authenticated(account)
        if not content.strip():
            raise DomainError("内容不能为空", code="empty_content")
        world = self.get_world(account, world_id)
        joined = self.store.connection.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone()
        if not joined:
            raise PermissionDenied("请先加入世界", code="not_a_participant")
        if request_id:
            row = self.store.connection.execute("SELECT * FROM events WHERE world_id=? AND request_id=?", (world_id, request_id)).fetchone()
            if row:
                if not self._event_visible(row, account.id):
                    raise PermissionDenied("该请求结果不可见", code="event_forbidden")
                return self._event_from_row(row)
            pending = self.store.connection.execute(
                "SELECT 1 FROM pending_interactions WHERE world_id=? AND request_id=? LIMIT 1", (world_id, request_id)
            ).fetchone()
            if pending:
                return self._interaction_request_status(account, world_id, request_id)
        if world.status == WorldStatus.ENDED:
            raise Conflict("世界已结束", code="world_ended")
        if world.status != WorldStatus.ACTIVE:
            raise Conflict("世界已暂停", code="world_paused")
        if not world.current:
            raise Conflict("世界已切换，请返回世界列表重新加入", code="world_switched")
        if expected_version is not None and world.state_version != expected_version:
            raise Conflict(f"世界状态已更新，请基于版本 {world.state_version} 重新提交", code="stale_state")
        self._ensure_model_request_allowed(world_id)
        return None

    def _bot_reply(self, account: Account, world: World, content: str, *, purpose: str, request_id: str) -> tuple[str, int]:
        # 深层设定和世界内部状态永远不传给 Bot 玩家。
        rows = self.store.connection.execute("SELECT * FROM events WHERE world_id=? AND kind!='system' ORDER BY sequence DESC", (world.id,)).fetchall()
        recent = [self._event_from_row(row) for row in rows if self._event_visible(row, "bot")][:12]
        context = "\n".join(event.content for event in reversed(recent))
        recalled = self._recall(account, world.id, content, viewer_id="bot", request_id=f"{request_id}:memory")
        if recalled:
            context = f"相关世界经历：\n{recalled}\n近期事件：\n{context}"
        self._ensure_model_request_allowed(world.id)
        settings = world.settings
        if self.bot_mode == "astrbot":
            if not self.bot_bridge:
                raise ModelExecutionError("AstrBot 连接器未配置", code="bot_disconnected")
            try:
                bridge_request = {
                    "request_id": request_id,
                    "purpose": purpose,
                    "world_id": world.id,
                    "account_id": account.id,
                    "display_name": str(settings.get("bot_character_name") if settings.get("role_mode") == "character" else settings.get("bot_name", "Bot")),
                    "content": content,
                    "visible_context": context,
                    "shallow_worldview": str(settings.get("shallow_worldview", "")),
                    "role_mode": str(settings.get("role_mode", "self")),
                    "character_name": str(settings.get("bot_character_name", "")),
                    "character_persona": str(settings.get("bot_character_persona", "")),
                }
                if purpose == "chat":
                    conversation_origin = self.qq_conversation_origin(account.id)
                    if conversation_origin:
                        bridge_request["conversation_origin"] = conversation_origin
                reservation_id = self._reserve_quota(
                    account,
                    world.id,
                    "bot",
                    _estimated_tokens(bridge_request, output_limit=2048),
                    request_id,
                )
                try:
                    result = self.bot_bridge.request(bridge_request)
                except BaseException:
                    self._release_quota_reservation(reservation_id)
                    raise
            except RuntimeError as exc:
                raise ModelExecutionError(str(exc), code="bot_disconnected") from exc
            if not isinstance(result, dict):
                self._release_quota_reservation(reservation_id)
                raise ModelExecutionError("AstrBot 连接器返回格式无效", code="bot_failed")
            usage = result.get("actual_tokens")
            if isinstance(usage, int) and not isinstance(usage, bool) and usage > 0:
                self._settle_quota_reservation(account, reservation_id, world.id, "bot", usage)
                self._record_model_usage_support("bot", True)
            else:
                self._release_quota_reservation(reservation_id)
                if result.get("usage_response_received") is True:
                    self._record_model_usage_support("bot", False)
            if result.get("error"):
                raise ModelExecutionError(str(result["error"]), code="bot_failed")
            if not isinstance(result.get("actual_tokens"), int) or isinstance(result["actual_tokens"], bool) or result["actual_tokens"] <= 0:
                raise ModelExecutionError("AstrBot 没有返回实际 Token 用量", code="usage_unavailable")
            return str(result.get("text", "")).strip(), result["actual_tokens"]
        if not self.chat_provider:
            raise ModelExecutionError("尚未配置 Bot 模型", code="bot_unconfigured")
        role_mode = str(settings.get("role_mode", "self"))
        character_context = ""
        if role_mode == "character":
            character_context = (
                f"\n你固定操控的角色：{settings.get('bot_character_name', '')}"
                f"\n该角色的人设：{settings.get('bot_character_persona', '')}"
                "\n你仍以自己的 Bot 身份思考和聊天；世界行动只由该角色执行，不得切换或操控其他角色。"
            )
        system = (
            "你是虚拟域中的平等 Bot 玩家，世界由独立模拟器裁定，你不能宣布行动已成功。保持自身人格。\n"
            f"Bot 人设：{settings.get('bot_persona', '')}\n"
            f"扮演模式：{role_mode}{character_context}\n"
            f"浅层世界设定：{settings.get('shallow_worldview', '')}\n"
            f"你可见的经历：{context}\n"
        )
        if purpose == "action":
            system += "提出你自己的下一次行动，只输出一句行动描述，不输出世界结算，不控制用户。"
        elif purpose == "interaction_response":
            system += "你正在回应另一位玩家对你的行动或请求。按你的人设和关系决定说什么或做什么，只输出你的回应内容，不裁定世界结果，也不替对方行动。"
        elif purpose == "autonomous_action":
            system += "用户离线，你可以按自身意愿决定独自行动。只输出一个低风险的自己的行动；不想行动时只输出 <VD_PASS>。不能给离线用户造成危险、永久损害或重大决定。"
        else:
            system += "这是游戏外聊天，不推进世界，不替用户行动。"
        try:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
            max_output_tokens = getattr(self.chat_provider.config, "max_output_tokens", None)
            if max_output_tokens is None:
                max_output_tokens = 1024
            response = self._complete_model_call(
                account,
                world.id,
                "bot",
                request_id,
                self.chat_provider,
                messages,
                max_tokens=max_output_tokens,
            )
        except ModelProviderError as exc:
            raise ModelExecutionError(str(exc), code="bot_model_failed") from exc
        return response.text.strip(), response.total_tokens

    def _bot_turn(self, account: Account, world_id: str, request_id: str) -> None:
        world = self.get_world(account, world_id)
        if world.status != WorldStatus.ACTIVE:
            return
        try:
            action, _usage = self._bot_reply(account, world, "请根据刚才世界中的变化，决定你自己下一步做什么。", purpose="action", request_id=request_id)
            if self.quota_snapshot(world_id).exhausted():
                return
            self._simulate_action(account, world_id, action, request_id=request_id + ":world", expected_version=world.state_version, bot_actor=True)
        except DomainError as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "bot.turn.failed", world_id, "failed", {"message": str(exc)})
            self._append_system_event(world_id, f"Bot 行动未完成：{exc}", request_id + ":failure")

    def _append_system_event(self, world_id: str, content: str, request_id: str) -> None:
        with self.store.transaction() as conn:
            row = conn.execute("SELECT state_version FROM worlds WHERE id=?", (world_id,)).fetchone()
            runtime = conn.execute("SELECT event_sequence FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()
            seq = int(runtime["event_sequence"]) + 1 if runtime is not None else 1
            conn.execute("INSERT OR IGNORE INTO events(id,world_id,sequence,kind,content,state_version,request_id,created_at) VALUES(?,?,?,'system',?,?,?,?)", (str(uuid.uuid4()), world_id, seq, content, row[0], request_id, utc_now()))
            if runtime is not None:
                conn.execute("UPDATE world_runtime SET event_sequence=? WHERE world_id=?", (seq, world_id))

    def tick(self) -> None:
        """A finite offline opportunity, never an unbounded model loop."""
        if not self.simulation_engine or not self._play_lock.acquire(blocking=False):
            return
        try:
            row = self.store.connection.execute("SELECT * FROM worlds WHERE is_current=1").fetchone()
            if not row:
                return
            world = self._world_from_row(row)
            if world.status == WorldStatus.ACTIVE and self._process_expired_interactions(world.id):
                return
            now = self._clock()
            self.runtime.advance_clock(world, now)
            players = self.runtime.presence(world.id, now)
            settings = world.settings
            if world.mode != WorldMode.FLOWING or world.status != WorldStatus.ACTIVE or not settings.get("bot_turn_enabled", False) or not settings.get("offline_bot_enabled", False) or not players or any(player["online"] for player in players) or self.quota_snapshot(world.id).exhausted():
                return
            if self.bot_mode == "astrbot" and not (self.bot_bridge and getattr(self.bot_bridge, "connected", False)):
                return
            day = now.astimezone(world_timezone(str(settings.get("timezone", "Asia/Shanghai")))).date().isoformat()
            with self.store.transaction() as conn:
                runtime = conn.execute("SELECT * FROM world_runtime WHERE world_id=?", (world.id,)).fetchone()
                if runtime["bot_day"] != day:
                    conn.execute("UPDATE world_runtime SET bot_day=?,bot_questions=0,bot_actions=0 WHERE world_id=?", (day, world.id))
                    runtime = dict(runtime) | {"bot_questions": 0, "bot_actions": 0}
                if runtime["next_bot_at"] and datetime.fromisoformat(runtime["next_bot_at"]) > now:
                    return
                if runtime["bot_questions"] >= int(settings.get("offline_daily_questions", 12)) or runtime["bot_actions"] >= int(settings.get("offline_daily_actions", 6)):
                    return
                next_at = now + timedelta(seconds=int(settings.get("offline_interval_seconds", 1800)))
                conn.execute("UPDATE world_runtime SET bot_questions=bot_questions+1,next_bot_at=? WHERE world_id=?", (timestamp(next_at), world.id))
            owner = self.store.connection.execute("SELECT a.* FROM accounts a JOIN participants p ON p.account_id=a.id WHERE p.world_id=? AND p.kind='human' LIMIT 1", (world.id,)).fetchone()
            account = Account(owner["id"], owner["username"], bool(owner["is_admin"]), owner["created_at"])
            request_id = f"offline:{world.id}:{day}:{int(runtime['bot_questions']) + 1}"
            self._offline_inflight = world.id
            try:
                action, _usage = self._bot_reply(account, world, "用户目前离线。你想在虚拟域中自己做什么？不想继续时输出 <VD_PASS>。", purpose="autonomous_action", request_id=request_id)
                if "<VD_PASS>" in action or not action.strip() or any(player["online"] for player in self.runtime.presence(world.id, self._clock())) or self.quota_snapshot(world.id).exhausted():
                    return
                with self.store.transaction() as conn:
                    conn.execute("UPDATE world_runtime SET bot_actions=bot_actions+1 WHERE world_id=?", (world.id,))
                self._simulate_action(account, world.id, action, request_id=request_id + ":world", expected_version=world.state_version, bot_actor=True, trigger="autonomous")
            except DomainError as exc:
                with self.store.transaction() as conn:
                    self._audit_conn(conn, account.id, "bot.offline.failed", world.id, "failed", {"message": str(exc)})
                self._append_system_event(world.id, f"Bot 离线行动未完成：{exc}", request_id + ":failure")
        finally:
            self._offline_inflight = None
            self._play_lock.release()

    def _recall(
        self,
        account: Account,
        world_id: str,
        query: str,
        *,
        viewer_id: str | None,
        request_id: str | None = None,
    ) -> str:
        if not self.memory:
            return ""
        call_index = 0

        def reserve(estimated_tokens: int) -> str:
            nonlocal call_index
            call_index += 1
            return self._reserve_quota(
                account,
                world_id,
                "memory",
                estimated_tokens,
                f"{request_id or uuid.uuid4()}:memory:{call_index}",
            )

        try:
            return self.memory.search(
                world_id,
                query,
                lambda used: self.record_usage(account, world_id, "memory", used),
                check=lambda: self._ensure_model_request_allowed(world_id),
                viewer_id=viewer_id,
                reserve=reserve,
                settle=lambda reservation_id, used: self._settle_quota_reservation(account, reservation_id, world_id, "memory", used),
                release=self._release_quota_reservation,
            )
        except (RuntimeError, KeyError, ValueError, IndexError) as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "memory.search.failed", world_id, "failed", {"message": str(exc)})
            raise ModelExecutionError(str(exc), code="memory_failed") from exc

    def _index_experience(self, account: Account, event: Event) -> None:
        if not self.memory or self.quota_snapshot(event.world_id).exhausted():
            return
        reservation_id = f"{event.request_id}:memory-index"
        try:
            self.memory.save(
                event,
                lambda used: self.record_usage(account, event.world_id, "memory", used),
                reserve=lambda estimated: self._reserve_quota(account, event.world_id, "memory", estimated, reservation_id),
                settle=lambda reserved_id, used: self._settle_quota_reservation(account, reserved_id, event.world_id, "memory", used),
                release=self._release_quota_reservation,
            )
        except (DomainError, RuntimeError, KeyError, ValueError) as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "memory.save.failed", event.world_id, "failed", {"message": str(exc)})
            self._append_system_event(event.world_id, f"世界已保存，但记忆索引失败，请管理员检查：{exc}", event.request_id + ":memory-failure")

    def _simulate_action(
        self,
        account: Account,
        world_id: str,
        content: str,
        *,
        request_id: str | None,
        expected_version: int | None,
        bot_actor: bool = False,
        trigger: str = "action",
        interaction_responses: list[dict[str, Any]] | None = None,
        allow_pending_interactions: bool = True,
    ) -> Event | dict[str, Any]:
        self._require_authenticated(account)
        self._ensure_model_request_allowed(world_id)
        world = self.get_world(account, world_id)
        if not world.current or world.status != WorldStatus.ACTIVE:
            raise Conflict("世界已切换或暂停", code="world_unavailable")
        time_context = self.runtime.time_context(world, self._clock(), trigger=trigger)
        # Static worlds normally have no flowing-time context.  The trigger
        # still matters to the simulator (especially the administrator's
        # one-shot configuration application), so preserve it even when the
        # runtime returns an empty mapping.
        time_context = dict(time_context or {})
        time_context["trigger"] = trigger
        if self._apply_pending_world_configuration(world_id):
            world = self.get_world(account, world_id)
        runtime_epoch = self.store.connection.execute("SELECT generation_epoch FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()[0]
        if not world.current or world.status != WorldStatus.ACTIVE:
            raise Conflict("世界已切换或暂停", code="world_unavailable")
        settings = world.settings or {}
        # The simulator is the world adjudicator and intentionally recalls full world history.
        recalled = self._recall(account, world_id, content, viewer_id=None, request_id=f"{request_id or 'world'}:memory")
        self._ensure_model_request_allowed(world_id)
        role_mode = str(settings.get("role_mode", "self"))
        participant_rows = self.store.connection.execute(
            "SELECT account_id,display_name,kind,persona_json FROM participants WHERE world_id=?", (world_id,)
        ).fetchall()
        presence_by_id = {str(item["account_id"]): item for item in self.runtime.presence(world_id, self._clock())}
        participants = []
        for row in participant_rows:
            profile = self.store.decode(row["persona_json"]) or {}
            participants.append({
                "account_id": str(row["account_id"]),
                "display_name": str(row["display_name"]),
                "kind": str(row["kind"]),
                "online": bool(presence_by_id.get(str(row["account_id"]), {}).get("online", False)),
                "role_mode": str(profile.get("role_mode", role_mode)),
                "player_persona": str(profile.get("player_persona", settings.get("player_persona", ""))),
                "character_name": str(profile.get("character_name", row["display_name"])),
                "character_persona": str(profile.get("character_persona", settings.get("player_persona", ""))),
                "view_scope": "仅本角色与周围可观察信息",
            })
        if settings.get("bot_name"):
            bot_character = role_mode == "character"
            participants.append({
                "account_id": "bot",
                "display_name": str(settings.get("bot_character_name") if bot_character else settings["bot_name"]),
                "kind": "bot",
                "online": bool(self.bot_mode != "astrbot" or (self.bot_bridge and getattr(self.bot_bridge, "connected", False))),
                "role_mode": role_mode,
                "player_persona": str(settings.get("bot_persona", "")),
                "character_name": str(settings.get("bot_character_name") if bot_character else settings["bot_name"]),
                "character_persona": str(settings.get("bot_character_persona") if bot_character else settings.get("bot_persona", "")),
                "view_scope": "仅本角色与周围可观察信息",
            })
        response_context = interaction_responses or []
        if bot_actor:
            actor_label = "Bot" if role_mode == "self" else f"Bot 操控的角色 {settings.get('bot_character_name', '')}"
            actor_persona = str(settings.get("bot_character_persona") if role_mode == "character" else settings.get("bot_persona", ""))
        elif trigger == "admin_once":
            actor_label = "管理员授权的一次世界推演"
            actor_persona = ""
        else:
            own = next((item for item in participants if item["account_id"] == account.id), {})
            actor_label = str(own.get("display_name") or account.username)
            if role_mode == "character":
                actor_label = f"玩家操控的角色 {actor_label}"
            actor_persona = str(own.get("character_persona") or own.get("player_persona") or settings.get("player_persona", ""))
        request = SimulationInput(
            world_id=world.id,
            state_version=world.state_version,
            world_state=world.state,
            action=content,
            visible_context=(str(settings.get("visible_context") or "玩家视角仅能知道自己的角色和周围可观察信息。") + f"\n参演模式：{role_mode}\n行动者：{actor_label}\n行动者人设：{actor_persona}\n参与者与固定角色：{json.dumps(participants, ensure_ascii=False)}\n相关经历：{recalled}"),
            shallow_worldview=str(settings.get("shallow_worldview") or settings.get("worldview") or ""),
            deep_worldview=str(settings.get("deep_worldview") or ""),
            time_context=time_context,
            consequence_rules={"death": bool(settings.get("allow_death", False)), "permanent_negative": bool(settings.get("allow_permanent_negative", False)), "long_term": bool(settings.get("allow_long_term", False))},
            participants=participants,
            interaction_responses=response_context,
            actor_id="bot" if bot_actor else account.id,
            actor_name=actor_label,
            state_schema=settings.get("state_schema", {}),
        )
        call_presence: dict[str, bool] = {}

        def before_simulation_call() -> None:
            self._ensure_model_request_allowed(world_id)
            call_presence.clear()
            call_presence.update({
                str(item["account_id"]): bool(item.get("online"))
                for item in request.participants
            })

        def reserve_simulation_call(messages: list[dict[str, str]], max_tokens: int, attempt: int) -> str:
            return self._reserve_model_call(
                account,
                world_id,
                "world",
                f"{request_id or 'world'}:attempt:{attempt + 1}",
                messages,
                max_tokens,
            )

        def settle_simulation_call(reservation_id: str, actual_tokens: int) -> None:
            self._settle_quota_reservation(account, reservation_id, world_id, "world", actual_tokens)

        def validate_returned_simulation(simulation_input: SimulationInput, simulation_result: SimulationResult) -> None:
            if trigger == "admin_once" and any(
                isinstance(effect, dict)
                and any(
                    effect.get(key) is True
                    for key in (
                        "dangerous_situation",
                        "death",
                        "permanent_negative",
                        "long_term",
                    )
                )
                for effect in simulation_result.player_effects or []
            ):
                raise SimulationError(
                    "管理员授权的一次性推演不能变更玩家状态",
                    code="admin_simulation_player_effect",
                )
            participants_by_id = {
                str(item["account_id"]): item
                for item in simulation_input.participants
            }
            death_targets = {
                str(effect.get("target_id", ""))
                for effect in simulation_result.player_effects or []
                if isinstance(effect, dict) and effect.get("death") is True
            }
            if len(participants_by_id) == 1:
                sole_participant_id = next(iter(participants_by_id))
                if sole_participant_id in death_targets and not simulation_result.world_end_declared:
                    raise SimulationError(
                        "唯一参演者死亡时，世界模型必须明确判断是否存在复活方式",
                        code="invalid_world_ending",
                    )
            if simulation_result.world_end:
                if len(participants_by_id) != 1:
                    raise SimulationError("只有唯一参演者的世界可以因角色死亡而结束", code="invalid_world_ending")
                sole_participant_id = next(iter(participants_by_id))
                if sole_participant_id not in death_targets:
                    raise SimulationError("世界终局必须对应唯一参演者的死亡", code="invalid_world_ending")
                if not simulation_result.ending_reason or len(simulation_result.ending_reason) > 1000:
                    raise SimulationError("世界终局必须提供不超过 1000 字的结束原因", code="invalid_world_ending")
                if simulation_result.pending_interactions or simulation_result.retry_required:
                    raise SimulationError("世界终局不能同时等待玩家回应或要求重新提交", code="invalid_world_ending")
            elif simulation_result.ending_reason:
                raise SimulationError("未结束的世界不能携带结束原因", code="invalid_world_ending")
            latest_players = self.runtime.presence(world_id, self._clock())
            latest_by_id = {str(player["account_id"]): player for player in latest_players}
            refreshed_participants = [
                dict(item) | {
                    "online": bool(latest_by_id.get(str(item["account_id"]), {}).get("online", item.get("online", False)))
                }
                for item in simulation_input.participants
            ]
            newly_offline = {
                str(item["account_id"])
                for item in refreshed_participants
                if call_presence.get(str(item["account_id"]), False) and not item["online"]
            }
            simulation_input.participants[:] = refreshed_participants
            for effect in simulation_result.player_effects or []:
                if not isinstance(effect, dict) or str(effect.get("target_id", "")) not in newly_offline:
                    continue
                if any(
                    effect.get(key) is True
                    for key in ("dangerous_situation", "death", "permanent_negative", "long_term")
                ):
                    raise SimulationError(
                        "玩家在推演期间离线，拒绝提交可能造成危险或重大后果的结果",
                        code="offline_protection",
                        retryable=False,
                    )
            if simulation_result.pending_interactions:
                if not allow_pending_interactions or trigger != "action":
                    raise SimulationError("该推演阶段不允许继续创建待回应交互", code="nested_interaction")
                actor_id = "bot" if bot_actor else account.id
                self._validate_pending_interaction_proposals(
                    simulation_result.pending_interactions,
                    refreshed_participants,
                    actor_id=actor_id,
                    action=content,
                )
            timeline = simulation_input.time_context
            if timeline.get("players"):
                simulation_input.time_context = dict(timeline) | {
                    "players": [
                        dict(item) | {
                            "online": bool(latest_by_id.get(str(item["account_id"]), {}).get("online", item.get("online", False)))
                        }
                        for item in timeline["players"]
                    ]
                }
            self.simulation_engine.validate_consequences(simulation_input, simulation_result)
            self.simulation_engine.validate_narrative_consequences(simulation_input, simulation_result)

        try:
            result = self.simulation_engine.run(
                request,
                before_call=before_simulation_call,
                reserve_call=reserve_simulation_call,
                settle_call=settle_simulation_call,
                release_call=self._release_quota_reservation,
                validate_result=validate_returned_simulation,
            )  # type: ignore[union-attr]
        except SimulationError as exc:
            raise ModelExecutionError(str(exc), code=exc.code) from exc
        actual_tokens = result.actual_tokens
        if actual_tokens is None:
            raise ModelExecutionError("世界模型没有返回实际 Token 用量", code="usage_unavailable")
        if result.retry_required:
            if allow_pending_interactions:
                raise ModelExecutionError("世界模型错误地要求玩家重新提交", code="invalid_simulation_result")
            reason = result.retry_reason or "等待回应期间，世界状态已变化，原行动不再适用。请重新确定行动。"
            self._append_system_event(world_id, reason, request_id + ":interaction-invalidated")
            return {"status": "resubmit_required", "request_id": request_id, "message": reason}
        if result.pending_interactions:
            if not allow_pending_interactions or trigger != "action":
                raise ModelExecutionError("该推演阶段不允许继续创建待回应交互", code="nested_interaction")
            return self._create_pending_interactions(
                account,
                world,
                content,
                request_id or str(uuid.uuid4()),
                result.pending_interactions,
                participants,
                actor_is_bot=bot_actor,
            )
        patch = dict(result.state_patch)
        if result.time_advance:
            patch["world_time"] = result.time_advance
        if result.memory_entries:
            memories = list(world.state.get("memory_entries", []))
            memories.extend(result.memory_entries)
            patch["memory_entries"] = memories[-200:]
        display_name = account.username
        row = self.store.connection.execute(
            "SELECT display_name FROM participants WHERE world_id=? AND account_id=?",
            (world_id, account.id),
        ).fetchone()
        if row and row["display_name"]:
            display_name = row["display_name"]
        if bot_actor:
            if role_mode == "character":
                display_name = f"Bot（操控角色：{settings.get('bot_character_name', '')}）"
            else:
                display_name = str(settings.get("bot_name") or "Bot")
        elif role_mode == "character":
            display_name = f"玩家（操控角色：{display_name}）"
        if response_context:
            response_lines = []
            for response in response_context:
                source = "（超时后由世界模拟器代为回应）" if response.get("source") == "simulator" else ""
                response_lines.append(f"{response.get('display_name', '玩家')}{source}：{response.get('content') or '未作回应，由世界模拟器按人设处理。'}")
            content = f"{content}\n\n待回应玩家的回应：\n" + "\n".join(response_lines)
        if trigger == "return":
            narrative = f"离开期间\n\n世界屏幕：{result.narrative}"
        elif trigger == "admin_once":
            narrative = f"管理员授权的一次世界推演\n\n世界屏幕：{result.narrative}"
        else:
            narrative = f"{display_name} 的行动：{content}\n\n世界屏幕：{result.narrative}"
        if result.world_end:
            narrative = f"{narrative}\n\n世界已结束：{result.ending_reason}"
        # Recheck presence after the model returns in both world modes. A model
        # decision started while a player was online cannot land after disconnect.
        latest_players = self.runtime.presence(world_id, self._clock())
        latest_by_id = {str(player["account_id"]): player for player in latest_players}
        request.participants = [dict(item) | {"online": bool(latest_by_id.get(str(item["account_id"]), {}).get("online", item.get("online", False)))} for item in request.participants]
        original = request.time_context
        if original.get("players"):
            request.time_context = dict(original) | {
                "players": [dict(item) | {"online": bool(latest_by_id.get(str(item["account_id"]), {}).get("online", False))} for item in original["players"]]
            }
        try:
            self.simulation_engine.validate_consequences(request, result)
            self.simulation_engine.validate_narrative_consequences(request, result)
        except SimulationError as exc:
            raise ModelExecutionError(str(exc), code=exc.code) from exc
        finally:
            request.time_context = original
        try:
            event = self._append_event(
                account,
                world_id,
                EventKind.TIME if trigger in {"return", "admin_once"} else EventKind.ACTION,
                narrative,
                request_id,
                expected_version,
                actual_tokens,
                "world",
                state_patch=patch,
                bot_actor=bot_actor,
                time_context=time_context,
                prepaid_tokens=True,
                runtime_epoch=runtime_epoch,
                visible_to=self._simulation_audience(result, account.id, bot_actor=bot_actor),
                ending_reason=result.ending_reason if result.world_end else None,
            )
        except DomainError:
            raise
        self._index_experience(account, event)
        return event

    def _generate_chat(
        self,
        account: Account,
        world_id: str,
        content: str,
        *,
        request_id: str | None,
        expected_version: int | None,
    ) -> Event:
        self._require_authenticated(account)
        self._ensure_model_request_allowed(world_id)
        world = self.get_world(account, world_id)
        settings = world.settings or {}
        reply, usage = self._bot_reply(account, world, content, purpose="chat", request_id=request_id or str(uuid.uuid4()))
        player = self.store.connection.execute(
            "SELECT display_name FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)
        ).fetchone()
        player_name = str(player["display_name"]) if player and player["display_name"] else account.username
        bot_name = str(settings.get("bot_character_name") if settings.get("role_mode") == "character" else settings.get("bot_name", "Bot"))
        if settings.get("role_mode") == "character":
            bot_prefix = f"Bot（操控角色：{bot_name}）"
        else:
            bot_prefix = bot_name
        combined = f"{player_name}：{content}\n{bot_prefix}：{reply}"
        return self._append_event(
            account,
            world_id,
            EventKind.CHAT,
            combined,
            request_id,
            expected_version,
            usage,
            "bot",
            prepaid_tokens=True,
            visible_to=[account.id, "bot"],
        )

    def _append_event(
        self,
        account: Account,
        world_id: str,
        kind: EventKind,
        content: str,
        request_id: str | None,
        expected_version: int | None,
        actual_tokens: int,
        ledger: str,
        state_patch: dict[str, Any] | None = None,
        bot_actor: bool = False,
        time_context: dict | None = None,
        prepaid_tokens: bool = False,
        runtime_epoch: int | None = None,
        visible_to: list[str] | None = None,
        ending_reason: str | None = None,
    ) -> Event:
        self._require_authenticated(account)
        if not content.strip():
            raise DomainError("内容不能为空", code="empty_content")
        if actual_tokens < 0:
            raise DomainError("Token 用量不能为负数", code="invalid_token_usage")
        if ending_reason is not None and (not ending_reason.strip() or len(ending_reason.strip()) > 1000):
            raise DomainError("世界结束原因必须在 1 至 1000 个字符之间", code="invalid_world_ending")
        request_id = request_id or str(uuid.uuid4())
        now = utc_now()
        with self.store.transaction() as conn:
            world_row = conn.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
            if world_row is None:
                raise NotFound("世界不存在", code="world_not_found")
            world = self._world_from_row(world_row)
            if world.status == WorldStatus.ENDED:
                raise Conflict("世界已结束，不能继续提交请求", code="world_ended")
            if world.status == WorldStatus.PAUSED:
                raise Conflict("世界已暂停", code="world_paused")
            if not world.current:
                raise Conflict("世界已切换", code="world_switched")
            if conn.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone() is None and not account.is_admin:
                raise PermissionDenied("账号尚未加入该世界", code="not_a_participant")
            duplicate = conn.execute("SELECT * FROM events WHERE world_id=? AND request_id=?", (world_id, request_id)).fetchone()
            if duplicate:
                if not self._event_visible(duplicate, account.id):
                    raise PermissionDenied("该请求结果不可见", code="event_forbidden")
                return self._event_from_row(duplicate)
            if expected_version is not None and expected_version != world.state_version:
                raise Conflict(f"世界状态已更新，请基于版本 {world.state_version} 重新提交", code="stale_state")
            quota = self._quota_from_connection(conn, world_id)
            if runtime_epoch is not None and conn.execute("SELECT generation_epoch FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()[0] != runtime_epoch:
                raise Conflict("世界运行状态已变化，本次推演结果已丢弃", code="stale_runtime")
            if quota.exhausted() and not prepaid_tokens:
                raise QuotaExceeded("额度已耗尽，世界已暂停请求", code="quota_exhausted")
            # 游戏聊天属于对话事件，不应改变世界推演状态版本；否则玩家刚
            # 聊天后提交行动会被无意义地判定为过期状态。
            new_version = world.state_version if kind == EventKind.CHAT else world.state_version + 1
            runtime_row = conn.execute(
                "SELECT event_sequence FROM world_runtime WHERE world_id=?",
                (world_id,),
            ).fetchone()
            sequence = int(runtime_row["event_sequence"]) + 1 if runtime_row is not None else 1
            event_id = str(uuid.uuid4())
            audience = list(dict.fromkeys(str(item) for item in (visible_to if visible_to is not None else ["*"])))
            state = dict(world.state)
            if state_patch:
                state.update(state_patch)
            if kind != EventKind.CHAT:
                state["last_event"] = {"kind": kind.value, "actor_id": None if bot_actor else account.id, "content": content, "sequence": sequence}
                state["event_count"] = int(state.get("event_count", 0)) + 1
            if ending_reason is not None:
                state["world_end"] = {
                    "reason": ending_reason.strip(),
                    "event_id": event_id,
                    "sequence": sequence,
                    "ended_at": now,
                }
            conn.execute(
                "INSERT INTO events(id,world_id,sequence,kind,actor_id,content,state_version,token_usage,request_id,created_at,visibility_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, world_id, sequence, kind.value, None if bot_actor else account.id, content, new_version, actual_tokens, request_id, now, json.dumps(audience, ensure_ascii=False)),
            )
            conn.execute(
                "UPDATE world_runtime SET event_sequence=? WHERE world_id=?",
                (sequence, world_id),
            )
            new_status = WorldStatus.ENDED.value if ending_reason is not None else world.status.value
            conn.execute("UPDATE worlds SET status=?,state_version=?,state_json=?,updated_at=? WHERE id=?", (new_status, new_version, self.store.encode(state), now, world_id))
            if ending_reason is not None:
                conn.execute(
                    "UPDATE pending_interactions SET status='cancelled' WHERE world_id=? AND status IN ('pending','ready','resolving')",
                    (world_id,),
                )
                conn.execute(
                    "UPDATE world_runtime SET generation_epoch=generation_epoch+1,next_bot_at=NULL WHERE world_id=?",
                    (world_id,),
                )
            if time_context and "active_seconds" in time_context:
                total = time_context["active_seconds"]
                conn.execute("UPDATE world_runtime SET settled_seconds=? WHERE world_id=?", (total, world_id))
                if bot_actor:
                    conn.execute("UPDATE world_runtime SET bot_interaction_seconds=? WHERE world_id=?", (total, world_id))
                else:
                    conn.execute("UPDATE player_activity SET interaction_seconds=? WHERE world_id=? AND account_id=?", (total, world_id, account.id))
            if actual_tokens and not prepaid_tokens:
                column = {"bot": "bot_used", "world": "world_used", "memory": "memory_used"}[ledger]
                conn.execute(f"UPDATE quota_limits SET {column}={column}+? WHERE world_id=?", (actual_tokens, world_id))
            if kind != EventKind.CHAT:
                self._insert_world_node_conn(
                    conn,
                    world_id,
                    event_id=event_id,
                    sequence=sequence,
                    state_version=new_version,
                    state_json=self.store.encode(state),
                    status=new_status,
                    mode=world_row["mode"],
                    settings_json=world_row["settings_json"],
                    created_at=now,
                )
            return Event(event_id, world_id, sequence, kind, None if bot_actor else account.id, content, new_version, actual_tokens, now, request_id, audience)

    @staticmethod
    def _validate_pending_interaction_proposals(
        items: list[dict[str, Any]],
        participants: list[dict[str, Any]],
        *,
        actor_id: str,
        action: str,
    ) -> None:
        if not items or len(items) > 8:
            raise SimulationError("待回应交互数量必须在 1 到 8 项之间", code="invalid_interaction")
        known = {str(item["account_id"]): item for item in participants}
        seen_targets: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise SimulationError("待回应交互格式错误", code="invalid_interaction")
            target_id = str(item.get("target_id", "")).strip()
            target = known.get(target_id)
            if not target or target_id == actor_id or target_id in seen_targets:
                raise SimulationError("待回应交互引用了未知、自身或重复玩家", code="invalid_interaction")
            seen_targets.add(target_id)
            risk = str(item.get("risk_level", "normal")).lower()
            dangerous = item.get("dangerous", False)
            if risk not in {"low", "normal", "high"} or not isinstance(dangerous, bool):
                raise SimulationError("待回应交互缺少有效风险标记", code="invalid_interaction")
            prompt = str(item.get("prompt", "有玩家的行动需要你回应。请作出回应。")).strip()[:1000]
            risk_text = f"{action} {prompt}"
            lexical_danger = contains_dangerous_situation(risk_text)
            if not target.get("online") and (dangerous or risk == "high" or lexical_danger):
                raise SimulationError(
                    "目标玩家离线，世界模拟器拒绝危险互动",
                    code="offline_protection",
                    retryable=False,
                )

    def _create_pending_interactions(
        self,
        account: Account,
        world: World,
        action: str,
        request_id: str,
        items: list[dict[str, Any]],
        participants: list[dict[str, Any]],
        *,
        actor_is_bot: bool = False,
    ) -> dict[str, Any]:
        actor_kind = "bot" if actor_is_bot else "human"
        self._validate_pending_interaction_proposals(
            items,
            participants,
            actor_id="bot" if actor_is_bot else account.id,
            action=action,
        )
        known = {str(item["account_id"]): item for item in participants}
        timeout = int(world.settings.get("interaction_timeout_seconds", 300))
        now = self._clock()
        created = []
        with self.store.transaction() as conn:
            for item in items:
                target_id = str(item.get("target_id", "")).strip()
                target = known.get(target_id)
                prompt = str(item.get("prompt", "有玩家的行动需要你回应。请作出回应。" )).strip()[:1000]
                deadline = now + timedelta(seconds=timeout)
                interaction_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO pending_interactions(id,world_id,request_id,actor_id,actor_kind,target_id,target_name,action,prompt,created_at,deadline_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'pending')",
                    (interaction_id, world.id, request_id, account.id, actor_kind, target_id, str(target.get("display_name", target_id)), action, prompt, timestamp(now), timestamp(deadline)),
                )
                created.append({"id": interaction_id, "target_id": target_id, "target_name": str(target.get("display_name", target_id)), "prompt": prompt})
            self._audit_conn(conn, account.id, "interaction.started", world.id, "pending", {"request_id": request_id, "targets": [item["target_id"] for item in created], "timeout_seconds": timeout})
        # The Bot is an online equal player. Ask it through its configured full pipeline; a disconnected AstrBot is handled by the same deadline takeover as any offline player.
        for item in created:
            if item["target_id"] == "bot" and self.bot_mode != "astrbot" or item["target_id"] == "bot" and self.bot_bridge and getattr(self.bot_bridge, "connected", False):
                self._bot_interaction_response(world, request_id, item["id"], action, item["prompt"])
        status = self._interaction_request_status(account, world.id, request_id)
        status["message"] = "行动已暂存，等待目标玩家回应；倒计时已经开始。"
        return status

    def _bot_interaction_response(self, world: World, request_id: str, interaction_id: str, action: str, prompt: str) -> None:
        row = self.store.connection.execute("SELECT * FROM pending_interactions WHERE id=? AND status='pending'", (interaction_id,)).fetchone()
        if row is None:
            return
        owner_row = self.store.connection.execute("SELECT id,username,is_admin,created_at FROM accounts WHERE id=?", (row["actor_id"],)).fetchone()
        if owner_row is None:
            return
        account = Account(owner_row["id"], owner_row["username"], bool(owner_row["is_admin"]), owner_row["created_at"])
        try:
            text, _used = self._bot_reply(account, world, f"玩家行动：{action}\n需要你回应的内容：{prompt}", purpose="interaction_response", request_id=f"{request_id}:bot-response")
            with self.store.transaction() as conn:
                conn.execute("UPDATE pending_interactions SET response_text=?,response_source='bot',status='responded' WHERE id=? AND status='pending'", (text, interaction_id))
            self._complete_interaction_request(world.id, request_id)
        except DomainError as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "interaction.bot_response.failed", interaction_id, "failed", {"message": str(exc)})

    def _interaction_request_status(self, account: Account, world_id: str, request_id: str, *, include_resolved: bool = True) -> dict[str, Any]:
        rows = self.store.connection.execute(
            "SELECT * FROM pending_interactions WHERE world_id=? AND request_id=? ORDER BY created_at,id", (world_id, request_id)
        ).fetchall()
        visible = [row for row in rows if account.is_admin or row["actor_id"] == account.id or row["target_id"] == account.id]
        if not visible:
            raise PermissionDenied("没有权限查看这项待回应交互", code="interaction_forbidden")
        now = self._clock()
        interactions = []
        for row in visible:
            if not include_resolved and row["status"] != "pending":
                continue
            deadline = datetime.fromisoformat(row["deadline_at"])
            interactions.append({"id": row["id"], "request_id": row["request_id"], "actor_id": row["actor_id"], "actor_kind": row["actor_kind"], "target_id": row["target_id"], "target_name": row["target_name"], "action": row["action"], "prompt": row["prompt"], "created_at": row["created_at"], "deadline_at": row["deadline_at"], "seconds_remaining": max(0, int((deadline - now).total_seconds())), "status": row["status"], "response_text": row["response_text"], "response_source": row["response_source"]})
        pending = any(row["status"] == "pending" for row in visible)
        resolving = any(row["status"] == "ready" for row in visible)
        status = "waiting_for_response" if pending else "resolving" if resolving else (visible[0]["status"] if visible else "resolved")
        return {"status": status, "request_id": request_id, "pending_interactions": interactions}

    def interactions(self, account: Account, world_id: str) -> dict[str, Any]:
        self._require_authenticated(account)
        self.get_world(account, world_id)
        if not account.is_admin and not self.store.connection.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone():
            raise PermissionDenied("账号尚未加入该世界", code="not_a_participant")
        rows = self.store.connection.execute(
            "SELECT DISTINCT request_id FROM pending_interactions WHERE world_id=? AND status IN ('pending','ready') AND (actor_id=? OR target_id=?) ORDER BY created_at",
            (world_id, account.id, account.id),
        ).fetchall()
        groups = [self._interaction_request_status(account, world_id, row["request_id"], include_resolved=False) for row in rows]
        return {"interactions": groups}

    def respond_interaction(self, account: Account, world_id: str, interaction_id: str, content: str) -> dict[str, Any]:
        self._require_authenticated(account)
        content = content.strip()
        if not content or len(content) > 2000:
            raise DomainError("回应内容长度必须在 1 至 2000 个字符之间", code="invalid_interaction_response")
        with self._play_lock:
            world = self.get_world(account, world_id)
            if not world.current or world.status != WorldStatus.ACTIVE:
                raise Conflict("当前世界不可回应，请等世界恢复并切换回来", code="world_unavailable")
            row = self.store.connection.execute("SELECT * FROM pending_interactions WHERE id=? AND world_id=?", (interaction_id, world_id)).fetchone()
            if row is None:
                raise NotFound("待回应交互不存在", code="interaction_not_found")
            if row["target_id"] != account.id:
                raise PermissionDenied("只有被通知的玩家可以回应", code="interaction_forbidden")
            if row["status"] != "pending":
                return self._interaction_request_status(account, world_id, row["request_id"])
            if datetime.fromisoformat(row["deadline_at"]) <= self._clock():
                with self.store.transaction() as conn:
                    conn.execute("UPDATE pending_interactions SET status='timed_out',response_source='simulator' WHERE id=? AND status='pending'", (interaction_id,))
                try:
                    return self._complete_interaction_request(world_id, row["request_id"])
                except DomainError:
                    with self.store.transaction() as conn:
                        conn.execute("UPDATE pending_interactions SET status='pending',deadline_at=? WHERE world_id=? AND request_id=? AND status='timed_out'", (timestamp(self._clock()), world_id, row["request_id"]))
                    raise
            self.runtime.touch(world_id, account.id, self._clock(), active=True)
            with self.store.transaction() as conn:
                conn.execute("UPDATE pending_interactions SET response_text=?,response_source='player',status='responded' WHERE id=? AND status='pending'", (content, interaction_id))
            return self._complete_interaction_request(world_id, row["request_id"])

    def _complete_interaction_request(self, world_id: str, request_id: str) -> dict[str, Any]:
        rows = self.store.connection.execute("SELECT * FROM pending_interactions WHERE world_id=? AND request_id=? ORDER BY created_at,id", (world_id, request_id)).fetchall()
        if not rows:
            raise NotFound("待回应交互不存在", code="interaction_not_found")
        if any(row["status"] == "pending" for row in rows):
            return {"status": "waiting_for_response", "request_id": request_id, "pending_interactions": [dict(row) for row in rows]}
        world_row = self.store.connection.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
        if world_row is None:
            raise NotFound("世界不存在", code="world_not_found")
        world = self._world_from_row(world_row)
        actor_row = self.store.connection.execute("SELECT id,username,is_admin,created_at FROM accounts WHERE id=?", (rows[0]["actor_id"],)).fetchone()
        if actor_row is None:
            raise NotFound("原行动者账号不存在", code="account_not_found")
        actor = Account(actor_row["id"], actor_row["username"], bool(actor_row["is_admin"]), actor_row["created_at"])
        responses = []
        for row in rows:
            responses.append({"target_id": row["target_id"], "display_name": row["target_name"], "content": row["response_text"], "source": "simulator" if row["status"] == "timed_out" else row["response_source"], "prompt": row["prompt"]})
        with self.store.transaction() as conn:
            conn.execute("UPDATE pending_interactions SET resolution_attempts=resolution_attempts+1 WHERE world_id=? AND request_id=?", (world_id, request_id))
            attempt_row = conn.execute("SELECT MAX(resolution_attempts) AS attempts FROM pending_interactions WHERE world_id=? AND request_id=?", (world_id, request_id)).fetchone()
        attempts = int(attempt_row["attempts"] or 1)
        try:
            outcome = self._simulate_action(
                actor,
                world_id,
                rows[0]["action"],
                request_id=request_id,
                expected_version=world.state_version,
                bot_actor=str(rows[0]["actor_kind"]) == "bot",
                interaction_responses=responses,
                allow_pending_interactions=False,
            )
        except DomainError as exc:
            with self.store.transaction() as conn:
                if attempts < 3:
                    conn.execute("UPDATE pending_interactions SET status='ready' WHERE world_id=? AND request_id=? AND status IN ('responded','timed_out','ready')", (world_id, request_id))
                    self._audit_conn(conn, actor.id, "interaction.resolve.retry", request_id, "retry", {"attempt": attempts, "message": str(exc)})
                else:
                    conn.execute("UPDATE pending_interactions SET status='failed' WHERE world_id=? AND request_id=? AND status IN ('responded','timed_out','ready')", (world_id, request_id))
                    self._audit_conn(conn, actor.id, "interaction.resolve.failed", request_id, "failed", {"attempts": attempts, "message": str(exc)})
            if attempts >= 3:
                self._append_system_event(world_id, f"待回应交互结算连续失败 3 次，请管理员处理：{exc}", request_id + ":resolve-failure")
                return {"status": "failed", "request_id": request_id, "message": "世界结算连续失败 3 次，已通知管理员；回应已保留。"}
            return {"status": "resolving", "request_id": request_id, "message": f"世界结算暂时失败（第 {attempts}/3 次），已保存回应并会自动重试。"}
        if isinstance(outcome, Event):
            with self.store.transaction() as conn:
                conn.execute("UPDATE pending_interactions SET status='resolved',resolved_event_id=? WHERE world_id=? AND request_id=?", (outcome.id, world_id, request_id))
            return {"status": "resolved", "request_id": request_id, "event": {"id": outcome.id, "sequence": outcome.sequence, "content": outcome.content}}
        with self.store.transaction() as conn:
            conn.execute("UPDATE pending_interactions SET status='invalidated' WHERE world_id=? AND request_id=?", (world_id, request_id))
        return outcome

    def _process_expired_interactions(self, world_id: str) -> bool:
        if self.quota_snapshot(world_id).exhausted():
            return False
        now = self._clock()
        with self.store.transaction() as conn:
            expired = conn.execute("SELECT id,request_id FROM pending_interactions WHERE world_id=? AND status='pending' AND deadline_at<=?", (world_id, timestamp(now))).fetchall()
            for row in expired:
                conn.execute("UPDATE pending_interactions SET status='timed_out',response_source='simulator' WHERE id=? AND status='pending'", (row["id"],))
            ready = conn.execute("SELECT DISTINCT request_id FROM pending_interactions WHERE world_id=? AND status='ready'", (world_id,)).fetchall()
        requests = {row["request_id"] for row in expired} | {row["request_id"] for row in ready}
        for request_id in requests:
            self._complete_interaction_request(world_id, request_id)
        return bool(requests)

    def events(self, account: Account, world_id: str, *, after_sequence: int = 0, limit: int = 100, management: bool = False) -> list[Event]:
        self._require_authenticated(account)
        self.get_world(account, world_id)
        if management and not account.is_admin:
            raise PermissionDenied("只有管理员创作连接可以读取完整事件", code="management_connection_required")
        # Direct service calls may come from trusted internal admin workflows. Both HTTP
        # adapters downgrade an ordinary admin login to a player Account before reaching here.
        full_access = management or account.is_admin
        if not full_access and not self.store.connection.execute("SELECT 1 FROM participants WHERE world_id=? AND account_id=?", (world_id, account.id)).fetchone():
            raise PermissionDenied("账号尚未加入该世界", code="not_a_participant")
        rows = self.store.connection.execute("SELECT * FROM events WHERE world_id=? AND sequence>? ORDER BY sequence", (world_id, after_sequence)).fetchall()
        if not full_access:
            rows = [row for row in rows if self._event_visible(row, account.id)]
        return [self._event_from_row(row) for row in rows[:max(1, min(limit, 500))]]

    def world_nodes(
        self,
        account: Account,
        world_id: str,
        *,
        before_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """List rollback checkpoints. This endpoint is restricted to management connections."""
        self._require_admin(account)
        if self.store.connection.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone() is None:
            raise NotFound("世界不存在", code="world_not_found")
        if before_sequence is not None and (
            isinstance(before_sequence, bool)
            or not isinstance(before_sequence, int)
            or before_sequence < 0
        ):
            raise DomainError("节点分页位置必须是非负整数", code="invalid_node_cursor")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
        ):
            raise DomainError("节点分页数量必须在 1 到 100 之间", code="invalid_node_limit")
        where = ["n.world_id=?"]
        params: list[Any] = [world_id]
        if before_sequence is not None:
            where.append("n.sequence<?")
            params.append(before_sequence)
        limit_clause = ""
        if limit is not None:
            limit_clause = " LIMIT ?"
            params.append(limit)
        rows = self.store.connection.execute(
            "SELECT n.id,n.sequence,n.state_version,n.status,n.mode,n.configuration_version,n.created_at,"
            "e.kind AS event_kind,e.content AS event_content "
            "FROM world_nodes n LEFT JOIN events e ON e.id=n.event_id "
            f"WHERE {' AND '.join(where)} ORDER BY n.sequence DESC{limit_clause}",
            params,
        ).fetchall()
        result = []
        for row in rows:
            content = str(row["event_content"] or "").strip().replace("\r", " ").replace("\n", " ")
            result.append(
                {
                    "node_id": row["id"],
                    "sequence": int(row["sequence"]),
                    "state_version": int(row["state_version"]),
                    "status": row["status"],
                    "mode": row["mode"],
                    "configuration_version": int(row["configuration_version"]),
                    "event_kind": row["event_kind"],
                    "summary": content[:240] if content else "世界初始状态",
                    "created_at": row["created_at"],
                }
            )
        return result

    def rollback_world(self, account: Account, world_id: str, node_id: str) -> dict[str, Any]:
        """Discard events after a saved node while keeping usage and versions monotonic."""
        self._require_admin(account)
        node_id = str(node_id).strip()
        if not node_id:
            raise DomainError("必须选择一个世界节点", code="invalid_world_node")
        with self._play_lock:
            now_dt = self._clock()
            now = timestamp(now_dt)
            request_id = f"rollback:{uuid.uuid4()}"
            with self.store.transaction() as conn:
                world = conn.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
                if world is None:
                    raise NotFound("世界不存在", code="world_not_found")
                node = conn.execute(
                    "SELECT * FROM world_nodes WHERE id=? AND world_id=?",
                    (node_id, world_id),
                ).fetchone()
                if node is None:
                    raise NotFound("世界节点不存在或不属于该世界", code="world_node_not_found")
                runtime = conn.execute("SELECT * FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()
                current_sequence = int(runtime["event_sequence"]) if runtime is not None else 0
                latest_event = conn.execute(
                    "SELECT COALESCE(MAX(sequence),0) FROM events WHERE world_id=?",
                    (world_id,),
                ).fetchone()[0]
                current_sequence = max(current_sequence, int(latest_event or 0))
                target_sequence = int(node["sequence"])
                if target_sequence > current_sequence:
                    raise Conflict("世界节点版本无效", code="invalid_world_node")
                try:
                    restored_state = self.store.decode(node["state_json"])
                    restored_settings = self.store.decode(node["settings_json"])
                    restored_runtime = self.store.decode(node["runtime_json"])
                    restored_status = WorldStatus(node["status"]).value
                    restored_mode = WorldMode(node["mode"]).value
                except (TypeError, ValueError) as exc:
                    raise Conflict("世界节点快照无法读取", code="invalid_world_node") from exc
                if not isinstance(restored_state, dict) or not isinstance(restored_settings, dict) or not isinstance(restored_runtime, dict):
                    raise Conflict("世界节点快照结构无效", code="invalid_world_node")
                try:
                    validate_settings(restored_settings)
                    validate_state_values(
                        restored_settings.get("state_schema", {}),
                        restored_state,
                        allow_reserved=True,
                    )
                except ValueError as exc:
                    raise Conflict("世界节点状态与配置不兼容，无法回滚", code="invalid_world_node") from exc

                target_config_version = int(node["configuration_version"])
                config = conn.execute(
                    "SELECT version FROM world_configuration_versions WHERE world_id=? AND version=?",
                    (world_id, target_config_version),
                ).fetchone()
                if config is None:
                    target_config_version = int(
                        conn.execute(
                            "SELECT COALESCE(MAX(version),0)+1 FROM world_configuration_versions WHERE world_id=?",
                            (world_id,),
                        ).fetchone()[0]
                    )
                    conn.execute(
                        "INSERT INTO world_configuration_versions(world_id,version,mode,settings_json,status,created_by,created_at,applied_at) "
                        "VALUES(?,?,?,?, 'active',?,?,?)",
                        (
                            world_id,
                            target_config_version,
                            restored_mode,
                            self.store.encode(restored_settings),
                            account.id,
                            now,
                            now,
                        ),
                    )
                conn.execute(
                    "UPDATE world_configuration_versions SET status='superseded' "
                    "WHERE world_id=? AND status IN ('active','pending')",
                    (world_id,),
                )
                conn.execute(
                    "UPDATE world_configuration_versions SET status='active',applied_at=COALESCE(applied_at,?) "
                    "WHERE world_id=? AND version=?",
                    (now, world_id, target_config_version),
                )

                discarded_events = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM events WHERE world_id=? AND sequence>?",
                        (world_id, target_sequence),
                    ).fetchone()[0]
                )
                cancelled = conn.execute(
                    "UPDATE pending_interactions SET status='cancelled' "
                    "WHERE world_id=? AND (status IN ('pending','ready','resolving','responded','timed_out','failed') "
                    "OR resolved_event_id IN (SELECT id FROM events WHERE world_id=? AND sequence>?))",
                    (world_id, world_id, target_sequence),
                )
                conn.execute("DELETE FROM world_nodes WHERE world_id=? AND sequence>?", (world_id, target_sequence))
                conn.execute("DELETE FROM events WHERE world_id=? AND sequence>?", (world_id, target_sequence))

                if runtime is None:
                    conn.execute(
                        "INSERT OR IGNORE INTO world_runtime(world_id,anchor_at,event_sequence) VALUES(?,?,?)",
                        (world_id, now, current_sequence),
                    )
                new_version = int(world["state_version"]) + 1
                conn.execute(
                    "UPDATE worlds SET mode=?,status=?,state_version=?,settings_json=?,state_json=?,updated_at=? WHERE id=?",
                    (
                        restored_mode,
                        restored_status,
                        new_version,
                        self.store.encode(restored_settings),
                        self.store.encode(restored_state),
                        now,
                        world_id,
                    ),
                )
                active_seconds = float(restored_runtime.get("active_seconds", 0))
                next_bot_at = None
                if (
                    restored_mode == WorldMode.FLOWING.value
                    and restored_status == WorldStatus.ACTIVE.value
                    and restored_settings.get("offline_bot_enabled")
                ):
                    next_bot_at = timestamp(
                        now_dt + timedelta(seconds=int(restored_settings.get("offline_interval_seconds", 1800)))
                    )
                conn.execute(
                    "UPDATE world_runtime SET anchor_at=?,active_seconds=?,settled_seconds=?,bot_interaction_seconds=?,"
                    "next_bot_at=?,generation_epoch=generation_epoch+1,event_sequence=? WHERE world_id=?",
                    (
                        now,
                        active_seconds,
                        float(restored_runtime.get("settled_seconds", 0)),
                        float(restored_runtime.get("bot_interaction_seconds", 0)),
                        next_bot_at,
                        current_sequence + 1,
                        world_id,
                    ),
                )
                conn.execute(
                    "UPDATE player_activity SET interaction_seconds=? WHERE world_id=?",
                    (active_seconds, world_id),
                )
                player_interaction_seconds = restored_runtime.get("player_interaction_seconds", {})
                if isinstance(player_interaction_seconds, dict):
                    for participant_id, interaction_seconds in player_interaction_seconds.items():
                        if not isinstance(interaction_seconds, (int, float)) or isinstance(interaction_seconds, bool):
                            continue
                        conn.execute(
                            "UPDATE player_activity SET interaction_seconds=? WHERE world_id=? AND account_id=?",
                            (float(interaction_seconds), world_id, str(participant_id)),
                        )

                rollback_event_id = str(uuid.uuid4())
                rollback_sequence = current_sequence + 1
                conn.execute(
                    "INSERT INTO events(id,world_id,sequence,kind,actor_id,content,state_version,token_usage,request_id,created_at,visibility_json) "
                    "VALUES(?,?,?,'system',?,?,?,0,?,?,?)",
                    (
                        rollback_event_id,
                        world_id,
                        rollback_sequence,
                        account.id,
                        f"管理员已将世界回滚到节点 {node_id[:8]}（原事件序号 {target_sequence}）。节点之后的世界历史和记忆已丢弃；已产生的 Token 用量保留。",
                        new_version,
                        request_id,
                        now,
                        '["*"]',
                    ),
                )
                self._insert_world_node_conn(
                    conn,
                    world_id,
                    event_id=rollback_event_id,
                    sequence=rollback_sequence,
                    state_version=new_version,
                    state_json=self.store.encode(restored_state),
                    status=restored_status,
                    mode=restored_mode,
                    settings_json=self.store.encode(restored_settings),
                    created_at=now,
                    configuration_version=target_config_version,
                )
                self._audit_conn(
                    conn,
                    account.id,
                    "world.rollback",
                    world_id,
                    "success",
                    {
                        "target_node_id": node_id,
                        "target_sequence": target_sequence,
                        "rollback_event_id": rollback_event_id,
                        "discarded_event_count": discarded_events,
                        "cancelled_interaction_count": int(cancelled.rowcount),
                        "from_state_version": int(world["state_version"]),
                        "to_state_version": new_version,
                        "from_event_sequence": current_sequence,
                        "to_event_sequence": rollback_sequence,
                        "from_status": world["status"],
                        "to_status": restored_status,
                        "token_usage_preserved": True,
                    },
                    request_id=request_id,
                )
            return {
                "ok": True,
                "world_id": world_id,
                "target_node_id": node_id,
                "target_sequence": target_sequence,
                "rollback_node_id": rollback_event_id,
                "discarded_event_count": discarded_events,
                "cancelled_interaction_count": int(cancelled.rowcount),
                "state_version": new_version,
                "event_sequence": rollback_sequence,
                "status": restored_status,
            }

    def backup_configuration(self, account: Account) -> dict[str, Any]:
        self._require_admin(account)
        row = self.store.connection.execute("SELECT automatic_enabled,interval_hours,retention_count,last_auto_at FROM backup_settings WHERE singleton=1").fetchone()
        return {"automatic_enabled": bool(row["automatic_enabled"]), "interval_hours": int(row["interval_hours"]), "retention_count": row["retention_count"], "last_auto_at": row["last_auto_at"]}

    def set_backup_configuration(self, account: Account, *, automatic_enabled: bool, interval_hours: int = 24, retention_count: int | None = None) -> dict[str, Any]:
        self._require_admin(account)
        if not isinstance(automatic_enabled, bool):
            raise DomainError("自动备份必须是开关值", code="invalid_backup_settings")
        if isinstance(interval_hours, bool) or not isinstance(interval_hours, int) or not 1 <= interval_hours <= 8760:
            raise DomainError("自动备份周期必须在 1 至 8760 小时之间", code="invalid_backup_settings")
        if retention_count is not None and (isinstance(retention_count, bool) or not isinstance(retention_count, int) or retention_count < 1):
            raise DomainError("保留数量必须是正整数或留空", code="invalid_backup_settings")
        if automatic_enabled and retention_count is None:
            raise DomainError("启用自动备份前必须设置保留数量", code="backup_retention_required")
        with self.store.transaction() as conn:
            conn.execute("UPDATE backup_settings SET automatic_enabled=?,interval_hours=?,retention_count=? WHERE singleton=1", (int(automatic_enabled), interval_hours, retention_count))
            self._audit_conn(conn, account.id, "backup.settings.update", "service", "success", {"automatic_enabled": automatic_enabled, "interval_hours": interval_hours, "retention_count": retention_count})
        return self.backup_configuration(account)

    @staticmethod
    def _scrub_backup_value(value: Any) -> Any:
        blocked = ("api_key", "password", "token", "secret", "credential", "private_key")
        if isinstance(value, dict):
            return {key: VirtualDomainService._scrub_backup_value(item) for key, item in value.items() if not any(term in str(key).lower() for term in blocked)}
        if isinstance(value, list):
            return [VirtualDomainService._scrub_backup_value(item) for item in value]
        return value

    def _backup_snapshot(self) -> dict[str, Any]:
        with self.store._lock:
            conn = self.store.connection
            worlds = conn.execute("SELECT * FROM worlds ORDER BY created_at,id").fetchall()
            snapshot: dict[str, Any] = {"format": "virtual-domain-backup", "format_version": 1, "created_at": utc_now(), "worlds": []}
            has_memories = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_memories'").fetchone() is not None
            for row in worlds:
                world_id = row["id"]
                world_data = {"world": {"id": row["id"], "name": row["name"], "mode": row["mode"], "status": row["status"], "state_version": row["state_version"], "is_current": bool(row["is_current"]), "settings": self._scrub_backup_value(self.store.decode(row["settings_json"])), "state": self._scrub_backup_value(self.store.decode(row["state_json"])), "created_at": row["created_at"], "updated_at": row["updated_at"]}}
                participants = []
                for item in conn.execute(
                    "SELECT p.account_id,p.kind,p.display_name,p.persona_json,a.username "
                    "FROM participants p LEFT JOIN accounts a ON a.id=p.account_id "
                    "WHERE p.world_id=? ORDER BY p.account_id",
                    (world_id,),
                ).fetchall():
                    participant = dict(item)
                    username = participant.pop("username", None)
                    participant["account_ref"] = {"username": username} if username else {}
                    persona = json.loads(participant["persona_json"] or "{}")
                    participant["persona_json"] = json.dumps(self._scrub_backup_value(persona), ensure_ascii=False, separators=(",", ":"))
                    participants.append(participant)
                world_data["participants"] = participants
                configuration_rows = conn.execute(
                    "SELECT version,mode,settings_json,status,created_by,created_at,applied_at FROM world_configuration_versions WHERE world_id=? ORDER BY version",
                    (world_id,),
                ).fetchall()
                if configuration_rows:
                    world_data["configuration_versions"] = [
                        {"version": int(item["version"]), "mode": item["mode"], "settings": self._scrub_backup_value(self.store.decode(item["settings_json"])), "status": item["status"], "created_by": item["created_by"], "created_at": item["created_at"], "applied_at": item["applied_at"]}
                        for item in configuration_rows
                    ]
                else:
                    world_data["configuration_versions"] = [{"version": 1, "mode": row["mode"], "settings": self._scrub_backup_value(self.store.decode(row["settings_json"])), "status": "active", "created_by": None, "created_at": row["created_at"], "applied_at": row["updated_at"]}]
                world_data["events"] = [dict(item) for item in conn.execute("SELECT id,sequence,kind,actor_id,content,state_version,token_usage,request_id,created_at,visibility_json FROM events WHERE world_id=? ORDER BY sequence", (world_id,)).fetchall()]
                world_data["nodes"] = []
                for item in conn.execute(
                    "SELECT id,world_id,event_id,sequence,state_version,state_json,status,mode,settings_json,"
                    "configuration_version,runtime_json,created_at FROM world_nodes WHERE world_id=? ORDER BY sequence",
                    (world_id,),
                ).fetchall():
                    node = dict(item)
                    node["state_json"] = json.dumps(
                        self._scrub_backup_value(self.store.decode(node["state_json"])),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    node["settings_json"] = json.dumps(
                        self._scrub_backup_value(self.store.decode(node["settings_json"])),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    node["runtime_json"] = json.dumps(
                        self.store.decode(node["runtime_json"]),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    world_data["nodes"].append(node)
                quota = conn.execute("SELECT * FROM quota_limits WHERE world_id=?", (world_id,)).fetchone()
                world_data["quota"] = dict(quota) if quota else None
                runtime = conn.execute("SELECT * FROM world_runtime WHERE world_id=?", (world_id,)).fetchone()
                world_data["runtime"] = dict(runtime) if runtime else None
                world_data["player_activity"] = [dict(item) for item in conn.execute("SELECT account_id,activity_at,transport_at,interaction_seconds,disconnected FROM player_activity WHERE world_id=?", (world_id,)).fetchall()]
                world_data["pending_interactions"] = [dict(item) for item in conn.execute("SELECT id,request_id,actor_id,actor_kind,target_id,target_name,action,prompt,created_at,deadline_at,status,response_text,response_source,resolution_attempts,resolved_event_id FROM pending_interactions WHERE world_id=?", (world_id,)).fetchall()]
                world_data["invitations"] = [dict(item) for item in conn.execute("SELECT id,world_id,code_hash,expires_at,redeemed_by,created_at FROM invitations WHERE world_id=? ORDER BY created_at,id", (world_id,)).fetchall()]
                if has_memories:
                    world_data["memories"] = [dict(item) for item in conn.execute("SELECT event_id,content,vector_json,embedding_model,visibility_json FROM world_memories WHERE world_id=? ORDER BY event_id", (world_id,)).fetchall()]
                snapshot["worlds"].append(world_data)
            account_ids = set()
            for world_data in snapshot["worlds"]:
                account_ids.update(self._backup_account_ids([world_data]))
            if account_ids:
                placeholders = ",".join("?" for _ in account_ids)
                rows = conn.execute(
                    f"SELECT id,username FROM accounts WHERE id IN ({placeholders})",
                    tuple(sorted(account_ids)),
                ).fetchall()
                snapshot["account_refs"] = {row["id"]: {"username": row["username"]} for row in rows}
            else:
                snapshot["account_refs"] = {}
            return snapshot

    @staticmethod
    def _backup_account_ids(worlds: list[dict[str, Any]]) -> set[str]:
        account_ids: set[str] = set()

        def add(value: Any) -> None:
            if isinstance(value, str) and value and value not in {"bot", "*"}:
                account_ids.add(value)

        def add_nested(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        key in {"account_id", "actor_id", "created_by", "target_id"}
                        or key.endswith("_account_id")
                        or key.endswith("_actor_id")
                    ):
                        if isinstance(item, str):
                            add(item)
                    elif key == "visible_to" and isinstance(item, list):
                        for observer in item:
                            add(observer)
                    else:
                        add_nested(item)
            elif isinstance(value, list):
                for item in value:
                    add_nested(item)

        for world_data in worlds:
            world = world_data.get("world", {})
            add_nested(world.get("settings", {}))
            add_nested(world.get("state", {}))
            for participant in world_data.get("participants", []):
                add(participant.get("account_id"))
                try:
                    add_nested(json.loads(participant.get("persona_json") or "{}"))
                except (TypeError, json.JSONDecodeError):
                    pass
            for version in world_data.get("configuration_versions", []):
                add(version.get("created_by"))
            for event in world_data.get("events", []):
                add(event.get("actor_id"))
                try:
                    for observer in json.loads(event.get("visibility_json", "[]")):
                        add(observer)
                except (TypeError, json.JSONDecodeError):
                    pass
            for node in world_data.get("nodes", []):
                for field in ("state_json", "settings_json"):
                    try:
                        add_nested(json.loads(node.get(field) or "{}"))
                    except (TypeError, json.JSONDecodeError):
                        pass
                try:
                    runtime = json.loads(node.get("runtime_json") or "{}")
                    add_nested(runtime)
                except (TypeError, json.JSONDecodeError):
                    pass
            for activity in world_data.get("player_activity", []):
                add(activity.get("account_id"))
            for interaction in world_data.get("pending_interactions", []):
                add(interaction.get("actor_id"))
                add(interaction.get("target_id"))
            for invitation in world_data.get("invitations", []):
                add(invitation.get("redeemed_by"))
            for memory in world_data.get("memories", []):
                try:
                    for observer in json.loads(memory.get("visibility_json", "[]")):
                        add(observer)
                except (TypeError, json.JSONDecodeError):
                    pass
        return account_ids

    def preview_backup_import(self, account: Account, snapshot: dict[str, Any]) -> dict[str, Any]:
        self._require_admin(account)
        self._validate_backup_snapshot(snapshot)
        worlds = snapshot["worlds"]
        source_ids = self._backup_account_ids(worlds)
        required_account_ids: set[str] = set()
        refs = snapshot.get("account_refs", {})
        roles: dict[str, list[dict[str, str]]] = {source_id: [] for source_id in source_ids}
        for world_data in worlds:
            world = world_data["world"]
            for participant in world_data.get("participants", []):
                source_id = str(participant["account_id"])
                roles.setdefault(source_id, []).append({
                    "world_id": str(world.get("id", "")),
                    "world_name": str(world.get("name", "")),
                    "display_name": str(participant.get("display_name", "")),
                    "kind": str(participant.get("kind", "player")),
                })
            for interaction in world_data.get("pending_interactions", []):
                if interaction.get("status") in {"pending", "ready", "resolving"}:
                    actor_id = str(interaction.get("actor_id", ""))
                    target_id = str(interaction.get("target_id", ""))
                    if actor_id:
                        required_account_ids.add(actor_id)
                    if target_id and target_id != "bot":
                        required_account_ids.add(target_id)
        accounts = []
        for source_id in sorted(source_ids):
            ref = refs.get(source_id, {}) if isinstance(refs, dict) else {}
            accounts.append({
                "source_account_id": source_id,
                "source_username": str(ref.get("username", "")) if isinstance(ref, dict) else "",
                "roles": roles.get(source_id, []),
                "mapping_required": source_id in required_account_ids,
            })
        return {
            "format_version": snapshot["format_version"],
            "world_count": len(worlds),
            "source_accounts": accounts,
            "target_accounts": self.admin_accounts(account),
            "world_id_conflicts": [
                {"source_world_id": str(item["world"]["id"]), "world_name": str(item["world"].get("name", ""))}
                for item in worlds
                if self.store.connection.execute("SELECT 1 FROM worlds WHERE id=?", (item["world"]["id"],)).fetchone()
            ],
        }

    @staticmethod
    def _remap_backup_value(
        value: Any,
        mappings: dict[str, str | None],
        *,
        reference_maps: dict[str, dict[str, str]] | None = None,
    ) -> Any:
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                reference_key = {
                    "world_id": "world",
                    "target_world_id": "world",
                    "event_id": "event",
                    "source_event_id": "event",
                    "resolved_event_id": "event",
                    "interaction_id": "interaction",
                    "pending_interaction_id": "interaction",
                    "invitation_id": "invitation",
                }.get(key)
                if reference_key and isinstance(item, str):
                    result[key] = (reference_maps or {}).get(reference_key, {}).get(item, item)
                    continue
                if key == "visible_to" and isinstance(item, list):
                    mapped = []
                    for observer in item:
                        if observer in {"bot", "*"}:
                            mapped.append(observer)
                        elif observer in mappings and mappings[observer] is not None:
                            mapped.append(mappings[observer])
                    result[key] = list(dict.fromkeys(mapped))
                elif (
                    key in {"account_id", "actor_id", "created_by"}
                    or key.endswith("_account_id")
                    or key.endswith("_actor_id")
                ) and isinstance(item, str):
                    result[key] = mappings.get(item)
                elif key == "target_id" and isinstance(item, str) and item != "bot":
                    result[key] = mappings.get(item)
                else:
                    result[key] = VirtualDomainService._remap_backup_value(item, mappings, reference_maps=reference_maps)
            return result
        if isinstance(value, list):
            return [VirtualDomainService._remap_backup_value(item, mappings, reference_maps=reference_maps) for item in value]
        return value

    @staticmethod
    def _remap_visibility_json(raw: str, mappings: dict[str, str | None]) -> str:
        try:
            values = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            values = []
        if not isinstance(values, list):
            values = []
        mapped = []
        for value in values:
            if value in {"bot", "*"}:
                mapped.append(value)
            elif value in mappings and mappings[value] is not None:
                mapped.append(mappings[value])
        return json.dumps(list(dict.fromkeys(mapped)), ensure_ascii=False)

    def _prepare_external_backup_import(
        self,
        snapshot: dict[str, Any],
        account_mappings: dict[str, str | None] | None,
    ) -> dict[str, Any]:
        source_ids = self._backup_account_ids(snapshot["worlds"])
        if account_mappings is None:
            raise DomainError(
                "导入 JSON 备份必须逐个确认备份中的玩家账号映射",
                code="account_mapping_required",
            )
        if not isinstance(account_mappings, dict) or set(account_mappings) != source_ids:
            raise DomainError(
                "必须逐个确认备份中的玩家账号映射",
                code="account_mapping_required",
            )
        mappings: dict[str, str | None] = {}
        for source_id, target_id in account_mappings.items():
            if target_id is None or target_id == "":
                mappings[source_id] = None
                continue
            if not isinstance(target_id, str):
                raise DomainError("账号映射目标无效", code="invalid_account_mapping")
            exists = self.store.connection.execute("SELECT 1 FROM accounts WHERE id=?", (target_id,)).fetchone()
            if not exists:
                raise DomainError("账号映射目标不存在", code="invalid_account_mapping")
            mappings[source_id] = target_id

        for world_data in snapshot["worlds"]:
            mapped_participants = [
                mappings.get(str(item["account_id"]))
                for item in world_data.get("participants", [])
                if mappings.get(str(item["account_id"])) is not None
            ]
            if len(mapped_participants) != len(set(mapped_participants)):
                raise DomainError("同一世界中的多个角色不能映射到同一个账号", code="duplicate_account_mapping")
            for interaction in world_data.get("pending_interactions", []):
                if interaction.get("status") in {"pending", "ready", "resolving"}:
                    actor = mappings.get(str(interaction.get("actor_id", "")))
                    target_id = str(interaction.get("target_id", ""))
                    target = "bot" if target_id == "bot" else mappings.get(target_id)
                    if actor is None or target is None:
                        raise DomainError(
                            "仍有待回应交互，必须先映射其发起方和回应方",
                            code="pending_interaction_mapping_required",
                        )

        world_id_map: dict[str, str] = {}
        for world_data in snapshot["worlds"]:
            source_world_id = str(world_data["world"]["id"])
            collision = self.store.connection.execute("SELECT 1 FROM worlds WHERE id=?", (source_world_id,)).fetchone()
            world_id_map[source_world_id] = str(uuid.uuid4()) if collision else source_world_id
        reference_maps: dict[str, dict[str, str]] = {"world": world_id_map}
        for ref_type, collection_name, table_name in (
            ("event", "events", "events"),
            ("node", "nodes", "world_nodes"),
            ("interaction", "pending_interactions", "pending_interactions"),
            ("invitation", "invitations", "invitations"),
        ):
            ref_map: dict[str, str] = {}
            seen: set[str] = set()
            for world_data in snapshot["worlds"]:
                for item in world_data.get(collection_name, []):
                    source_id = str(item["id"])
                    if source_id in seen:
                        raise DomainError(f"备份包含重复的 {ref_type} ID", code="invalid_backup_snapshot")
                    seen.add(source_id)
                    collision = self.store.connection.execute(f"SELECT 1 FROM {table_name} WHERE id=?", (source_id,)).fetchone()
                    ref_map[source_id] = str(uuid.uuid4()) if collision else source_id
            reference_maps[ref_type] = ref_map

        for world_data in snapshot["worlds"]:
            source_world_id = str(world_data["world"]["id"])
            world_data["world"] = self._remap_backup_value(world_data["world"], mappings, reference_maps=reference_maps)
            world_data["world"]["id"] = world_id_map[source_world_id]
            world_data["world"]["settings"] = self._remap_backup_value(world_data["world"].get("settings", {}), mappings, reference_maps=reference_maps)
            world_data["world"]["state"] = self._remap_backup_value(world_data["world"].get("state", {}), mappings, reference_maps=reference_maps)
            participants = []
            for participant in world_data.get("participants", []):
                mapped_id = mappings.get(str(participant.get("account_id", "")))
                if mapped_id is None:
                    continue
                item = dict(participant)
                item["account_id"] = mapped_id
                item.pop("account_ref", None)
                try:
                    persona = json.loads(item.get("persona_json") or "{}")
                except json.JSONDecodeError:
                    persona = {}
                item["persona_json"] = json.dumps(self._remap_backup_value(persona, mappings, reference_maps=reference_maps), ensure_ascii=False)
                participants.append(item)
            world_data["participants"] = participants
            for version in world_data.get("configuration_versions", []):
                creator = version.get("created_by")
                version["created_by"] = mappings.get(creator) if creator else None
                version["settings"] = self._remap_backup_value(version.get("settings", {}), mappings, reference_maps=reference_maps)
            for event in world_data.get("events", []):
                event["id"] = reference_maps["event"][str(event["id"])]
                actor = event.get("actor_id")
                source_actor = str(actor) if actor else None
                event["actor_id"] = mappings.get(actor) if actor else None
                if source_actor and event["actor_id"] is None:
                    account_ref = snapshot.get("account_refs", {}).get(source_actor, {})
                    label = account_ref.get("username") if isinstance(account_ref, dict) else None
                    event["content"] = f"[未映射历史账号{f'：{label}' if label else ''}] {event['content']}"
                event["visibility_json"] = self._remap_visibility_json(event.get("visibility_json", "[]"), mappings)
            for node in world_data.get("nodes", []):
                node["id"] = reference_maps["node"][str(node["id"])]
                node["world_id"] = world_id_map[source_world_id]
                node["event_id"] = (
                    reference_maps["event"].get(str(node.get("event_id")))
                    if node.get("event_id")
                    else None
                )
                for field in ("state_json", "settings_json"):
                    try:
                        value = json.loads(node.get(field) or "{}")
                    except (TypeError, json.JSONDecodeError):
                        value = {}
                    node[field] = json.dumps(
                        self._remap_backup_value(value, mappings, reference_maps=reference_maps),
                        ensure_ascii=False,
                    )
                try:
                    runtime = json.loads(node.get("runtime_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    runtime = {}
                if isinstance(runtime, dict):
                    player_interaction_seconds = runtime.get("player_interaction_seconds", {})
                    if isinstance(player_interaction_seconds, dict):
                        runtime["player_interaction_seconds"] = {
                            mappings[source_id]: value
                            for source_id, value in player_interaction_seconds.items()
                            if mappings.get(str(source_id)) is not None
                        }
                node["runtime_json"] = json.dumps(runtime, ensure_ascii=False)
            world_data["player_activity"] = [
                dict(item) | {"account_id": mappings[str(item["account_id"])]}
                for item in world_data.get("player_activity", [])
                if mappings.get(str(item.get("account_id", ""))) is not None
            ]
            pending = []
            for interaction in world_data.get("pending_interactions", []):
                actor = mappings.get(str(interaction.get("actor_id", "")))
                target = str(interaction.get("target_id", ""))
                mapped_target = "bot" if target == "bot" else mappings.get(target)
                if actor is None or mapped_target is None:
                    continue
                pending.append(
                    dict(interaction)
                    | {
                        "id": reference_maps["interaction"][str(interaction["id"])],
                        "actor_id": actor,
                        "target_id": mapped_target,
                        "resolved_event_id": reference_maps["event"].get(str(interaction.get("resolved_event_id")))
                        if interaction.get("resolved_event_id") else None,
                    }
                )
            world_data["pending_interactions"] = pending
            for invitation in world_data.get("invitations", []):
                redeemed_by = invitation.get("redeemed_by")
                invitation["id"] = reference_maps["invitation"][str(invitation["id"])]
                invitation["world_id"] = world_id_map[source_world_id]
                invitation["code_hash"] = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
                invitation["expires_at"] = timestamp(self._clock())
                invitation["redeemed_by"] = None
            for memory in world_data.get("memories", []):
                memory["event_id"] = reference_maps["event"].get(str(memory.get("event_id", "")), memory.get("event_id"))
                memory["world_id"] = world_id_map[source_world_id]
                memory["visibility_json"] = self._remap_visibility_json(memory.get("visibility_json", "[]"), mappings)
            world_data["world"]["status"] = "paused"
            world_data["world"]["is_current"] = False
        snapshot.pop("account_refs", None)
        return {
            "unmapped_account_ids": sorted(source_id for source_id, target_id in mappings.items() if target_id is None),
            "world_id_map": world_id_map,
            "reference_maps": reference_maps,
        }

    def create_backup(self, account: Account, *, kind: str = "manual") -> dict[str, Any]:
        self._require_admin(account)
        if kind not in {"manual", "automatic"}:
            raise DomainError("未知备份类型", code="invalid_backup_kind")
        with self._backup_lock:
            snapshot = self._backup_snapshot()
            backup_id = str(uuid.uuid4())
            now = self._clock().astimezone(UTC).replace(microsecond=0)
            stamp = now.strftime("%Y%m%d_%H%M%S")
            file_name = f"virtual_domain_{kind}_{stamp}_{backup_id[:8]}.json"
            backup_dir = (Path(self.store.path).resolve().parent / "backups").resolve()
            backup_dir.mkdir(parents=True, exist_ok=True)
            final_path = (backup_dir / file_name).resolve()
            if final_path.parent != backup_dir:
                raise DomainError("备份路径无效", code="invalid_backup_path")
            temp_path = final_path.with_suffix(final_path.suffix + ".partial")
            try:
                temp_path.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(temp_path, final_path)
            except OSError as exc:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
                with self.store.transaction() as conn:
                    self._audit_conn(conn, account.id, "backup.create", file_name, "failed", {"error": type(exc).__name__, "kind": kind})
                raise DomainError(f"备份创建失败：{exc}", code="backup_failed") from exc
            size_bytes = final_path.stat().st_size
            with self.store.transaction() as conn:
                conn.execute("INSERT INTO backups(id,scope,kind,file_name,created_at,size_bytes) VALUES(?,'service',?,?,?,?)", (backup_id, kind, file_name, timestamp(now), size_bytes))
                if kind == "automatic":
                    conn.execute("UPDATE backup_settings SET last_auto_at=? WHERE singleton=1", (timestamp(now),))
                self._audit_conn(conn, account.id, "backup.create", backup_id, "success", {"kind": kind, "size_bytes": size_bytes})
            if kind == "automatic":
                config = self.store.connection.execute("SELECT retention_count FROM backup_settings WHERE singleton=1").fetchone()
                self._apply_backup_retention(int(config["retention_count"]))
            return {"id": backup_id, "scope": "service", "kind": kind, "file_name": file_name, "created_at": timestamp(now), "size_bytes": size_bytes}

    def restore_backup(
        self,
        account: Account,
        backup_source: str | dict[str, Any],
        *,
        account_mappings: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        """Restore worlds from a saved or imported snapshot while keeping usage monotonic."""
        self._require_admin(account)
        external_import = isinstance(backup_source, dict)
        if external_import:
            snapshot = copy.deepcopy(backup_source)
            backup_id = "external-import"
            file_name = "导入的 JSON 备份"
        elif isinstance(backup_source, str):
            backup_id = backup_source
            backup_path, file_name = self.backup_download_path(account, backup_id)
            try:
                snapshot = json.loads(backup_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                with self.store.transaction() as conn:
                    self._audit_conn(conn, account.id, "backup.restore", backup_id, "failed", {"file_name": file_name, "error": type(exc).__name__})
                raise DomainError(f"读取备份失败：{exc}", code="backup_restore_failed") from exc
        else:
            raise DomainError("备份来源无效", code="invalid_backup_snapshot")
        try:
            restored_ids = self._validate_backup_snapshot(snapshot)
            import_mapping_result = self._prepare_external_backup_import(snapshot, account_mappings) if external_import else None
            if import_mapping_result:
                restored_ids = [str(item["world"]["id"]) for item in snapshot["worlds"]]
                unmapped_account_ids = import_mapping_result["unmapped_account_ids"]
            else:
                unmapped_account_ids = []
            with self._backup_lock, self._play_lock, self.store.transaction() as conn:
                # Preserve each world ledger's greatest known totals so rolling back
                # story state can never refund model usage already incurred.
                previous_usage = {
                    row["world_id"]: (int(row["bot_used"]), int(row["world_used"]), int(row["memory_used"]))
                    for row in conn.execute("SELECT world_id,bot_used,world_used,memory_used FROM quota_limits").fetchall()
                }
                previous_runtime = {
                    row["world_id"]: dict(row)
                    for row in conn.execute("SELECT * FROM world_runtime").fetchall()
                }
                previous_state_versions = {
                    row["id"]: int(row["state_version"])
                    for row in conn.execute("SELECT id,state_version FROM worlds").fetchall()
                }
                carried_usage: dict[str, dict[str, int]] = {}
                if not external_import and any(bool(item["world"].get("is_current")) for item in snapshot["worlds"]):
                    conn.execute("UPDATE worlds SET is_current=0")
                for world_data in snapshot["worlds"]:
                    world = world_data["world"]
                    world_id = world["id"]
                    if conn.execute("SELECT 1 FROM worlds WHERE id=?", (world_id,)).fetchone():
                        conn.execute("DELETE FROM worlds WHERE id=?", (world_id,))
                    state_version = int(world["state_version"])
                    if not external_import and world_id in previous_state_versions:
                        state_version = max(state_version, previous_state_versions[world_id] + 1)
                    conn.execute(
                        "INSERT INTO worlds(id,name,mode,status,state_version,is_current,settings_json,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (world_id, world["name"], world["mode"], "paused" if external_import else world["status"], state_version, 0 if external_import else int(bool(world["is_current"])), self.store.encode(self._scrub_backup_value(world["settings"])), self.store.encode(self._scrub_backup_value(world["state"])), world["created_at"], world["updated_at"]),
                    )
                    for participant in world_data["participants"]:
                        persona = json.loads(participant["persona_json"] or "{}")
                        if not isinstance(persona, dict):
                            raise DomainError("备份中的角色人设结构无效", code="invalid_backup_snapshot")
                        conn.execute(
                            "INSERT INTO participants(world_id,account_id,kind,display_name,persona_json,online,last_seen) VALUES(?,?,?,?,?,0,NULL)",
                            (world_id, participant["account_id"], participant["kind"], participant["display_name"], self.store.encode(self._scrub_backup_value(persona))),
                        )
                    for item in world_data["configuration_versions"]:
                        conn.execute(
                            "INSERT INTO world_configuration_versions(world_id,version,mode,settings_json,status,created_by,created_at,applied_at) VALUES(?,?,?,?,?,?,?,?)",
                            (world_id, int(item["version"]), item["mode"], self.store.encode(item["settings"]), item["status"], item.get("created_by"), item["created_at"], item.get("applied_at")),
                        )
                    for item in world_data["events"]:
                        conn.execute(
                            "INSERT INTO events(id,world_id,sequence,kind,actor_id,content,state_version,token_usage,request_id,created_at,visibility_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (item["id"], world_id, int(item["sequence"]), item["kind"], item.get("actor_id"), item["content"], int(item["state_version"]), int(item["token_usage"]), item["request_id"], item["created_at"], item.get("visibility_json", '["*"]')),
                        )
                    quota = world_data.get("quota")
                    if quota:
                        old = previous_usage.get(world_id, (0, 0, 0))
                        carried_usage[world_id] = {
                            "bot": max(0, old[0] - int(quota.get("bot_used", 0))),
                            "world": max(0, old[1] - int(quota.get("world_used", 0))),
                            "memory": max(0, old[2] - int(quota.get("memory_used", 0))),
                        }
                        conn.execute(
                            "INSERT INTO quota_limits(world_id,total_limit,bot_limit,world_limit,memory_limit,bot_used,world_used,memory_used) VALUES(?,?,?,?,?,?,?,?)",
                            (world_id, quota.get("total_limit"), quota.get("bot_limit"), quota.get("world_limit"), quota.get("memory_limit"), max(int(quota.get("bot_used", 0)), old[0]), max(int(quota.get("world_used", 0)), old[1]), max(int(quota.get("memory_used", 0)), old[2])),
                        )
                    runtime = world_data.get("runtime")
                    if runtime:
                        previous = previous_runtime.get(world_id)
                        # Start restored flowing time from now and bump the epoch so
                        # any in-flight model result based on the future state is discarded.
                        anchor_at = timestamp(self._clock())
                        bot_day = previous.get("bot_day") if previous and previous.get("bot_day") else runtime.get("bot_day")
                        bot_questions = max(int(runtime.get("bot_questions", 0)), int(previous.get("bot_questions", 0))) if previous and previous.get("bot_day") == bot_day else int(runtime.get("bot_questions", 0))
                        bot_actions = max(int(runtime.get("bot_actions", 0)), int(previous.get("bot_actions", 0))) if previous and previous.get("bot_day") == bot_day else int(runtime.get("bot_actions", 0))
                        next_bot_at = previous.get("next_bot_at") if previous and previous.get("next_bot_at") else runtime.get("next_bot_at")
                        generation_epoch = max(int(runtime.get("generation_epoch", 0)), int(previous.get("generation_epoch", -1)) + 1 if previous else 0)
                        event_sequence = max(
                            int(runtime.get("event_sequence", 0)),
                            int(previous.get("event_sequence", 0)) if previous else 0,
                            max((int(item.get("sequence", 0)) for item in world_data.get("events", [])), default=0),
                        )
                        conn.execute(
                            "INSERT INTO world_runtime(world_id,anchor_at,active_seconds,settled_seconds,bot_interaction_seconds,bot_day,bot_questions,bot_actions,next_bot_at,generation_epoch,event_sequence) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (world_id, anchor_at, runtime.get("active_seconds", 0), runtime.get("settled_seconds", 0), runtime.get("bot_interaction_seconds", 0), bot_day, bot_questions, bot_actions, next_bot_at, generation_epoch, event_sequence),
                        )
                    for item in world_data.get("player_activity", []):
                        conn.execute(
                            "INSERT INTO player_activity(world_id,account_id,activity_at,transport_at,interaction_seconds,disconnected) VALUES(?,?,?,?,?,1)",
                            (world_id, item["account_id"], item["activity_at"], item["transport_at"], item.get("interaction_seconds", 0)),
                        )
                    for item in world_data.get("nodes", []):
                        state = json.loads(item["state_json"])
                        settings = json.loads(item["settings_json"])
                        runtime_snapshot = json.loads(item.get("runtime_json") or "{}")
                        conn.execute(
                            "INSERT INTO world_nodes(id,world_id,event_id,sequence,state_version,state_json,status,mode,settings_json,configuration_version,runtime_json,created_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                item["id"],
                                world_id,
                                item.get("event_id"),
                                int(item["sequence"]),
                                int(item["state_version"]),
                                self.store.encode(self._scrub_backup_value(state)),
                                item["status"],
                                item["mode"],
                                self.store.encode(self._scrub_backup_value(settings)),
                                int(item.get("configuration_version", 1)),
                                self.store.encode(runtime_snapshot),
                                item["created_at"],
                            ),
                        )
                    if not world_data.get("nodes"):
                        latest_event = conn.execute(
                            "SELECT id,sequence FROM events WHERE world_id=? ORDER BY sequence DESC LIMIT 1",
                            (world_id,),
                        ).fetchone()
                        sequence = int(latest_event["sequence"]) if latest_event is not None else 0
                        conn.execute(
                            "UPDATE world_runtime SET event_sequence=MAX(event_sequence,?) WHERE world_id=?",
                            (sequence, world_id),
                        )
                        restored_world = conn.execute("SELECT * FROM worlds WHERE id=?", (world_id,)).fetchone()
                        self._insert_world_node_conn(
                            conn,
                            world_id,
                            event_id=str(latest_event["id"]) if latest_event is not None else None,
                            sequence=sequence,
                            state_version=int(restored_world["state_version"]),
                            state_json=restored_world["state_json"],
                            status=restored_world["status"],
                            mode=restored_world["mode"],
                            settings_json=restored_world["settings_json"],
                            created_at=restored_world["updated_at"],
                        )
                    for item in world_data.get("pending_interactions", []):
                        conn.execute(
                            "INSERT INTO pending_interactions(id,world_id,request_id,actor_id,actor_kind,target_id,target_name,action,prompt,created_at,deadline_at,status,response_text,response_source,resolution_attempts,resolved_event_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (item["id"], world_id, item["request_id"], item["actor_id"], item.get("actor_kind", "human"), item["target_id"], item["target_name"], item["action"], item["prompt"], item["created_at"], item["deadline_at"], item["status"], item.get("response_text"), item.get("response_source"), int(item.get("resolution_attempts", 0)), item.get("resolved_event_id")),
                        )
                    for item in world_data.get("invitations", []):
                        conn.execute(
                            "INSERT INTO invitations(id,world_id,code_hash,expires_at,redeemed_by,created_at) VALUES(?,?,?,?,?,?)",
                            (item["id"], world_id, item["code_hash"], item["expires_at"], item.get("redeemed_by"), item["created_at"]),
                        )
                    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='world_memories'").fetchone():
                        for item in world_data.get("memories", []):
                            conn.execute(
                                "INSERT INTO world_memories(event_id,world_id,content,vector_json,embedding_model,visibility_json) VALUES(?,?,?,?,?,?)",
                                (item["event_id"], world_id, item["content"], item["vector_json"], item["embedding_model"], item.get("visibility_json", '["*"]')),
                            )
                self._audit_conn(
                    conn,
                    account.id,
                    "backup.restore",
                    backup_id,
                    "success",
                    {
                        "file_name": file_name,
                        "world_ids": restored_ids,
                        "world_count": len(restored_ids),
                        "usage_preserved": True,
                        "usage_carried_forward": carried_usage,
                        "account_mappings": account_mappings if external_import else None,
                        "unmapped_account_ids": unmapped_account_ids,
                        "world_id_map": import_mapping_result["world_id_map"] if import_mapping_result else None,
                    },
                )
            result = {"ok": True, "restored_world_ids": restored_ids, "world_count": len(restored_ids), "file_name": file_name, "unmapped_account_ids": unmapped_account_ids}
            if import_mapping_result:
                result["world_id_map"] = import_mapping_result["world_id_map"]
            return result
        except DomainError as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "backup.restore", backup_id, "failed", {"file_name": file_name, "error": exc.code})
            raise
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "backup.restore", backup_id, "failed", {"file_name": file_name, "error": type(exc).__name__})
            raise DomainError(f"备份内容无法恢复：{exc}", code="invalid_backup_snapshot") from exc

    @staticmethod
    def _validate_backup_snapshot(snapshot: Any) -> list[str]:
        if not isinstance(snapshot, dict) or snapshot.get("format") != "virtual-domain-backup" or snapshot.get("format_version") != 1:
            raise DomainError("备份格式或版本不受支持", code="unsupported_backup_format")
        worlds = snapshot.get("worlds")
        if not isinstance(worlds, list):
            raise DomainError("备份缺少世界列表", code="invalid_backup_snapshot")
        ids: list[str] = []
        current_count = 0
        node_ids: set[str] = set()
        required_lists = ("participants", "configuration_versions", "events")
        for entry in worlds:
            if not isinstance(entry, dict) or not isinstance(entry.get("world"), dict):
                raise DomainError("备份中的世界结构无效", code="invalid_backup_snapshot")
            world = entry["world"]
            world_id = world.get("id")
            if not isinstance(world_id, str) or not world_id or world_id in ids:
                raise DomainError("备份包含空或重复的世界 ID", code="invalid_backup_snapshot")
            if not isinstance(world.get("settings"), dict) or not isinstance(world.get("state"), dict):
                raise DomainError("备份中的世界设置或状态无效", code="invalid_backup_snapshot")
            ids.append(world_id)
            current_count += int(bool(world.get("is_current")))
            for key in required_lists:
                if not isinstance(entry.get(key), list):
                    raise DomainError(f"备份中的 {key} 结构无效", code="invalid_backup_snapshot")
            if not isinstance(entry.get("quota"), dict) or not isinstance(entry.get("runtime"), dict):
                raise DomainError("备份中的额度或运行时状态结构无效", code="invalid_backup_snapshot")
            nodes = entry.get("nodes", [])
            if not isinstance(nodes, list):
                raise DomainError("备份中的节点列表结构无效", code="invalid_backup_snapshot")
            event_ids = {
                str(event.get("id"))
                for event in entry["events"]
                if isinstance(event, dict) and event.get("id")
            }
            for node in nodes:
                if not isinstance(node, dict):
                    raise DomainError("备份中的世界节点结构无效", code="invalid_backup_snapshot")
                node_id = node.get("id")
                if not isinstance(node_id, str) or not node_id or node_id in node_ids:
                    raise DomainError("备份包含空或重复的世界节点 ID", code="invalid_backup_snapshot")
                node_ids.add(node_id)
                for key in ("sequence", "state_version", "configuration_version"):
                    value = node.get(key)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise DomainError("备份中的世界节点版本无效", code="invalid_backup_snapshot")
                if node["configuration_version"] < 1:
                    raise DomainError("备份中的世界节点配置版本无效", code="invalid_backup_snapshot")
                if node.get("event_id") is not None and str(node["event_id"]) not in event_ids:
                    raise DomainError("备份中的世界节点引用了不存在的事件", code="invalid_backup_snapshot")
                if node.get("status") not in {item.value for item in WorldStatus}:
                    raise DomainError("备份中的世界节点状态无效", code="invalid_backup_snapshot")
                if node.get("mode") not in {item.value for item in WorldMode}:
                    raise DomainError("备份中的世界节点模式无效", code="invalid_backup_snapshot")
                try:
                    node_state = json.loads(node.get("state_json") or "")
                    node_settings = json.loads(node.get("settings_json") or "")
                    node_runtime = json.loads(node.get("runtime_json") or "")
                except (TypeError, json.JSONDecodeError) as exc:
                    raise DomainError("备份中的世界节点快照 JSON 无效", code="invalid_backup_snapshot") from exc
                if not isinstance(node_state, dict) or not isinstance(node_settings, dict) or not isinstance(node_runtime, dict):
                    raise DomainError("备份中的世界节点快照结构无效", code="invalid_backup_snapshot")
        if current_count > 1:
            raise DomainError("备份标记了多个当前世界", code="invalid_backup_snapshot")
        return ids

    def _apply_backup_retention(self, retention_count: int) -> None:
        rows = self.store.connection.execute("SELECT id,file_name FROM backups WHERE kind='automatic' ORDER BY created_at DESC,id DESC").fetchall()
        backup_dir = (Path(self.store.path).resolve().parent / "backups").resolve()
        for row in rows[max(0, retention_count):]:
            path = (backup_dir / row["file_name"]).resolve()
            if path.parent != backup_dir:
                continue
            try:
                path.unlink(missing_ok=True)
            except OSError:
                continue
            with self.store.transaction() as conn:
                conn.execute("DELETE FROM backups WHERE id=? AND kind='automatic'", (row["id"],))

    def backup_tick(self) -> None:
        row = self.store.connection.execute("SELECT automatic_enabled,interval_hours,retention_count,last_auto_at FROM backup_settings WHERE singleton=1").fetchone()
        if not row or not row["automatic_enabled"] or row["retention_count"] is None:
            return
        now = self._clock()
        if row["last_auto_at"]:
            last = datetime.fromisoformat(row["last_auto_at"])
            if now - last < timedelta(hours=int(row["interval_hours"])):
                return
        account_row = self.store.connection.execute("SELECT id,username,is_admin,created_at FROM accounts WHERE is_admin=1 ORDER BY created_at LIMIT 1").fetchone()
        if not account_row:
            return
        account = Account(account_row["id"], account_row["username"], True, account_row["created_at"])
        try:
            self.create_backup(account, kind="automatic")
        except DomainError as exc:
            with self.store.transaction() as conn:
                self._audit_conn(conn, account.id, "backup.automatic.failed", "service", "failed", {"message": str(exc)})

    def list_backups(self, account: Account) -> list[dict[str, Any]]:
        self._require_admin(account)
        rows = self.store.connection.execute("SELECT id,scope,kind,file_name,created_at,size_bytes FROM backups ORDER BY created_at DESC,id DESC").fetchall()
        return [dict(row) for row in rows]

    def backup_download_path(self, account: Account, backup_id: str) -> tuple[Path, str]:
        self._require_admin(account)
        row = self.store.connection.execute("SELECT file_name FROM backups WHERE id=?", (backup_id,)).fetchone()
        if row is None:
            raise NotFound("备份不存在", code="backup_not_found")
        backup_dir = (Path(self.store.path).resolve().parent / "backups").resolve()
        path = (backup_dir / row["file_name"]).resolve()
        if path.parent != backup_dir or not path.is_file():
            raise NotFound("备份文件不存在", code="backup_file_not_found")
        return path, row["file_name"]

    def delete_backup(self, account: Account, backup_id: str) -> None:
        self._require_admin(account)
        path, file_name = self.backup_download_path(account, backup_id)
        with self._backup_lock:
            path.unlink()
            with self.store.transaction() as conn:
                conn.execute("DELETE FROM backups WHERE id=?", (backup_id,))
                self._audit_conn(conn, account.id, "backup.delete", backup_id, "success", {"file_name": file_name})

    def audit_logs(self, account: Account, *, limit: int = 100) -> list[dict[str, Any]]:
        self._require_admin(account)
        rows = self.store.connection.execute(
            "SELECT id,actor_id,action,target,result,request_id,detail_json,created_at FROM audit_logs ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [
            {
                "id": row["id"],
                "actor_id": row["actor_id"],
                "action": row["action"],
                "target": row["target"],
                "result": row["result"],
                "request_id": row["request_id"],
                "detail": self.store.decode(row["detail_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def record_model_settings_update(
        self,
        account: Account,
        role: str,
        *,
        changed_fields: list[str],
        api_key_changed: bool,
    ) -> None:
        self._require_admin(account)
        if role not in {"world", "bot", "memory"}:
            raise DomainError("未知模型用途", code="invalid_model_role")
        detail = {
            "changed_fields": sorted(field for field in changed_fields if field != "api_key"),
            "api_key_changed": bool(api_key_changed),
        }
        with self.store.transaction() as conn:
            self._audit_conn(conn, account.id, "model.profile.update", role, "success", detail)

    @staticmethod
    def _event_visible(row: Any, viewer_id: str) -> bool:
        try:
            visibility = json.loads(row["visibility_json"] or '["*"]')
        except (KeyError, TypeError, ValueError):
            return False
        return isinstance(visibility, list) and ("*" in visibility or viewer_id in visibility)

    @staticmethod
    def _simulation_audience(result: Any, actor_id: str, *, bot_actor: bool) -> list[str]:
        audience = list(result.visible_to) if result.visible_to is not None else []
        actor_view = "bot" if bot_actor else actor_id
        if "*" not in audience and actor_view not in audience:
            audience.append(actor_view)
        return list(dict.fromkeys(audience))

    def _event_from_row(self, row: Any) -> Event:
        try:
            visible_to = json.loads(row["visibility_json"] or '["*"]')
        except (KeyError, TypeError, ValueError):
            visible_to = []
        return Event(row["id"], row["world_id"], row["sequence"], EventKind(row["kind"]), row["actor_id"], row["content"], row["state_version"], row["token_usage"], row["created_at"], row["request_id"], visible_to if isinstance(visible_to, list) else [])

    def _quota_from_connection(self, conn: Any, world_id: str) -> QuotaSnapshot:
        row = conn.execute("SELECT * FROM quota_limits WHERE world_id=?", (world_id,)).fetchone()
        return QuotaSnapshot(row["bot_used"], row["world_used"], row["memory_used"], row["bot_limit"], row["world_limit"], row["memory_limit"], row["total_limit"])

    @staticmethod
    def _audit_conn(
        conn: Any,
        actor_id: str,
        action: str,
        target: str,
        result: str,
        detail: dict[str, Any],
        *,
        request_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_logs(id,actor_id,action,target,result,request_id,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), actor_id, action, target, result, request_id or str(uuid.uuid4()), json.dumps(detail, ensure_ascii=False), utc_now()),
        )

    # ----- 权限 -------------------------------------------------------
    @staticmethod
    def _require_authenticated(account: Account) -> None:
        if not account or not account.id:
            raise AuthenticationError("需要登录")

    def _require_admin(self, account: Account) -> None:
        self._require_authenticated(account)
        if not account.is_admin:
            raise PermissionDenied("只有管理员可以执行此操作")

