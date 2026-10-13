"""SQLite 持久化层。

SQLite 是第一版的默认存储，所有写操作都在事务中完成。上层服务只通过
这里的少量原子方法修改世界版本，便于以后替换成 PostgreSQL。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS qq_bindings (
    account_id TEXT PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
    qq_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    session_origin TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS qq_binding_codes (
    code_hash TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    redeemed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_qq_binding_codes_account
    ON qq_binding_codes(account_id, expires_at);

CREATE TABLE IF NOT EXISTS service_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS management_sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS worlds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 0,
    is_current INTEGER NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS worldview_generation_requests (
    request_id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    prompt_hash TEXT NOT NULL,
    target_world_id TEXT REFERENCES worlds(id) ON DELETE SET NULL,
    billed_world_id TEXT REFERENCES worlds(id) ON DELETE SET NULL,
    actual_tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('pending','succeeded','failed')),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_configuration_versions (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    mode TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','pending','superseded')),
    created_by TEXT REFERENCES accounts(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    applied_at TEXT,
    PRIMARY KEY(world_id,version)
);

CREATE TABLE IF NOT EXISTS participants (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    display_name TEXT NOT NULL,
    persona_json TEXT NOT NULL DEFAULT '{}',
    online INTEGER NOT NULL DEFAULT 0,
    last_seen TEXT,
    PRIMARY KEY (world_id, account_id)
);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    actor_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
    content TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    token_usage INTEGER NOT NULL DEFAULT 0,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    visibility_json TEXT NOT NULL DEFAULT '["*"]',
    UNIQUE (world_id, sequence),
    UNIQUE (world_id, request_id)
);

CREATE TABLE IF NOT EXISTS world_nodes (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    event_id TEXT REFERENCES events(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    status TEXT NOT NULL,
    mode TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    configuration_version INTEGER NOT NULL,
    runtime_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(world_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_world_nodes_world_sequence
    ON world_nodes(world_id, sequence DESC);

CREATE TABLE IF NOT EXISTS quota_limits (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    total_limit INTEGER,
    bot_limit INTEGER,
    world_limit INTEGER,
    memory_limit INTEGER,
    bot_used INTEGER NOT NULL DEFAULT 0,
    world_used INTEGER NOT NULL DEFAULT 0,
    memory_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS quota_reservations (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    account_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    ledger TEXT NOT NULL CHECK(ledger IN ('bot','world','memory')),
    estimated_tokens INTEGER NOT NULL CHECK(estimated_tokens > 0),
    session_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_reservations_world
    ON quota_reservations(world_id,ledger);

CREATE TABLE IF NOT EXISTS quota_reservation_settlements (
    reservation_id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    ledger TEXT NOT NULL CHECK(ledger IN ('bot','world','memory')),
    actual_tokens INTEGER NOT NULL CHECK(actual_tokens >= 0),
    settled_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invitations (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    redeemed_by TEXT REFERENCES accounts(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_interactions (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    actor_kind TEXT NOT NULL DEFAULT 'human',
    target_id TEXT NOT NULL,
    target_name TEXT NOT NULL,
    action TEXT NOT NULL,
    prompt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    response_text TEXT,
    response_source TEXT,
    resolution_attempts INTEGER NOT NULL DEFAULT 0,
    resolved_event_id TEXT REFERENCES events(id) ON DELETE SET NULL,
    UNIQUE(world_id, request_id, target_id)
);
CREATE INDEX IF NOT EXISTS idx_pending_interactions_deadline
    ON pending_interactions(world_id, status, deadline_at);

CREATE TABLE IF NOT EXISTS backup_settings (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    automatic_enabled INTEGER NOT NULL DEFAULT 0,
    interval_hours INTEGER NOT NULL DEFAULT 24,
    retention_count INTEGER,
    last_auto_at TEXT
);
INSERT OR IGNORE INTO backup_settings(singleton,automatic_enabled,interval_hours,retention_count,last_auto_at) VALUES(1,0,24,NULL,NULL);

CREATE TABLE IF NOT EXISTS backups (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL DEFAULT 'service',
    kind TEXT NOT NULL CHECK(kind IN ('manual','automatic')),
    file_name TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    size_bytes INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    actor_id TEXT REFERENCES accounts(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    result TEXT NOT NULL,
    request_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_runtime (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    anchor_at TEXT NOT NULL,
    active_seconds REAL NOT NULL DEFAULT 0,
    settled_seconds REAL NOT NULL DEFAULT 0,
    bot_interaction_seconds REAL NOT NULL DEFAULT 0,
    bot_day TEXT,
    bot_questions INTEGER NOT NULL DEFAULT 0,
    bot_actions INTEGER NOT NULL DEFAULT 0,
    next_bot_at TEXT,
    generation_epoch INTEGER NOT NULL DEFAULT 0,
    event_sequence INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS player_activity (
    world_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    activity_at TEXT NOT NULL,
    transport_at TEXT NOT NULL,
    interaction_seconds REAL NOT NULL DEFAULT 0,
    disconnected INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(world_id, account_id),
    FOREIGN KEY(world_id, account_id) REFERENCES participants(world_id, account_id) ON DELETE CASCADE
);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = "virtual_domain.sqlite3") -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection_lock = threading.RLock()
        self._thread_connections: weakref.WeakKeyDictionary[threading.Thread, sqlite3.Connection] = weakref.WeakKeyDictionary()
        self._connections: dict[int, sqlite3.Connection] = {}
        self._thread_finalizers: list[weakref.finalize] = []
        self._request_connection: ContextVar[sqlite3.Connection | None] = ContextVar(
            f"virtual_domain_sqlite_connection_{id(self)}",
            default=None,
        )
        self._single_connection_mode = self.path == ":memory:"
        self._closed = False
        self._finalizer = weakref.finalize(
            self,
            SQLiteStore._close_connections,
            self._connections,
            self._connection_lock,
        )
        self._connection = self._new_connection()
        self._bind_thread_connection(threading.current_thread(), self._connection)
        connection = self._connection
        connection.executescript(SCHEMA)
        binding_columns = {row[1] for row in connection.execute("PRAGMA table_info(qq_bindings)").fetchall()}
        if "session_origin" not in binding_columns:
            connection.execute("ALTER TABLE qq_bindings ADD COLUMN session_origin TEXT NOT NULL DEFAULT ''")
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(events)").fetchall()}
        if "visibility_json" not in event_columns:
            connection.execute("""ALTER TABLE events ADD COLUMN visibility_json TEXT NOT NULL DEFAULT '["*"]'""")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(world_runtime)").fetchall()}
        if "generation_epoch" not in columns:
            connection.execute("ALTER TABLE world_runtime ADD COLUMN generation_epoch INTEGER NOT NULL DEFAULT 0")
        if "event_sequence" not in columns:
            connection.execute("ALTER TABLE world_runtime ADD COLUMN event_sequence INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "UPDATE world_runtime SET event_sequence=MAX(event_sequence, "
            "COALESCE((SELECT MAX(sequence) FROM events WHERE events.world_id=world_runtime.world_id),0))"
        )
        generation_columns = {row[1] for row in connection.execute("PRAGMA table_info(worldview_generation_requests)").fetchall()}
        if "target_world_id" not in generation_columns:
            connection.execute("ALTER TABLE worldview_generation_requests ADD COLUMN target_world_id TEXT REFERENCES worlds(id) ON DELETE SET NULL")
        if "billed_world_id" not in generation_columns:
            connection.execute("ALTER TABLE worldview_generation_requests ADD COLUMN billed_world_id TEXT REFERENCES worlds(id) ON DELETE SET NULL")
        if "actual_tokens" not in generation_columns:
            connection.execute("ALTER TABLE worldview_generation_requests ADD COLUMN actual_tokens INTEGER NOT NULL DEFAULT 0")
        interaction_columns = {row[1] for row in connection.execute("PRAGMA table_info(pending_interactions)").fetchall()}
        if "actor_kind" not in interaction_columns:
            connection.execute("ALTER TABLE pending_interactions ADD COLUMN actor_kind TEXT NOT NULL DEFAULT 'human'")
        if "resolution_attempts" not in interaction_columns:
            connection.execute("ALTER TABLE pending_interactions ADD COLUMN resolution_attempts INTEGER NOT NULL DEFAULT 0")
        connection.commit()

    def _new_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        with self._connection_lock:
            if self._closed:
                connection.close()
                raise RuntimeError("SQLite 存储已经关闭")
            self._connections[id(connection)] = connection
        return connection

    def _bind_thread_connection(self, thread: threading.Thread, connection: sqlite3.Connection) -> None:
        self._thread_connections[thread] = connection
        self._thread_finalizers.append(
            weakref.finalize(
                thread,
                SQLiteStore._release_connection,
                id(connection),
                self._connections,
                self._connection_lock,
            )
        )

    @staticmethod
    def _release_connection(
        connection_id: int,
        connections: dict[int, sqlite3.Connection],
        connection_lock: threading.RLock,
    ) -> None:
        with connection_lock:
            connection = connections.pop(connection_id, None)
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass

    @staticmethod
    def _close_connections(
        connections: dict[int, sqlite3.Connection],
        connection_lock: threading.RLock,
    ) -> None:
        with connection_lock:
            for connection in connections.values():
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            connections.clear()

    @property
    def connection(self) -> sqlite3.Connection:
        request_connection = self._request_connection.get()
        if request_connection is not None:
            return request_connection
        if self._single_connection_mode:
            return self._connection
        thread = threading.current_thread()
        with self._connection_lock:
            if self._closed:
                raise RuntimeError("SQLite 存储已经关闭")
            connection = self._thread_connections.get(thread)
            if connection is None:
                connection = self._new_connection()
                self._bind_thread_connection(thread, connection)
            return connection

    @contextmanager
    def connection_scope(self) -> Iterator[sqlite3.Connection]:
        """给一个请求分配独立连接，并在请求结束时关闭它。"""
        if self._single_connection_mode:
            connection = self._connection
            token: Token[sqlite3.Connection | None] = self._request_connection.set(connection)
            try:
                yield connection
            finally:
                self._request_connection.reset(token)
            return

        with self._connection_lock:
            if self._closed:
                raise RuntimeError("SQLite 存储已经关闭")
            connection = self._new_connection()
        token = self._request_connection.set(connection)
        try:
            yield connection
        finally:
            self._request_connection.reset(token)
            self._release_connection(id(connection), self._connections, self._connection_lock)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            connection = self.connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            with self._connection_lock:
                if self._closed:
                    return
                self._thread_connections.clear()
                self._closed = True
                self._finalizer()
                for finalizer in self._thread_finalizers:
                    finalizer.detach()
                self._thread_finalizers.clear()

    @staticmethod
    def encode(value: dict[str, Any]) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def decode(value: str) -> dict[str, Any]:
        return json.loads(value) if value else {}

