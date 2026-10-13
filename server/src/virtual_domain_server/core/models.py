"""虚拟域核心数据模型。

模型只使用标准库，服务端、桌面客户端和 AstrBot 连接器都可以复用，
避免把任何一层的框架对象写进世界存档。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import StrEnum
from typing import Any


class WorldMode(StrEnum):
    STATIC = "static"
    FLOWING = "flowing"


class WorldStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ENDED = "ended"


class ParticipantKind(StrEnum):
    HUMAN = "human"
    BOT = "bot"


class EventKind(StrEnum):
    ACTION = "action"
    CHAT = "chat"
    SYSTEM = "system"
    TIME = "time"


@dataclass(slots=True)
class Account:
    id: str
    username: str
    is_admin: bool = False
    created_at: str = ""


@dataclass(slots=True)
class World:
    id: str
    name: str
    mode: WorldMode
    status: WorldStatus = WorldStatus.ACTIVE
    state_version: int = 0
    current: bool = False
    settings: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Event:
    id: str
    world_id: str
    sequence: int
    kind: EventKind
    actor_id: str | None
    content: str
    state_version: int
    token_usage: int = 0
    created_at: str = ""
    request_id: str = ""
    visible_to: list[str] = field(default_factory=lambda: ["*"], repr=False, compare=False, metadata={"private": True})


@dataclass(slots=True)
class QuotaSnapshot:
    """三个额度账本的当前状态；None 表示没有配置该上限。"""

    bot_used: int = 0
    world_used: int = 0
    memory_used: int = 0
    bot_limit: int | None = None
    world_limit: int | None = None
    memory_limit: int | None = None
    total_limit: int | None = None
    reserved_bot: int = 0
    reserved_world: int = 0
    reserved_memory: int = 0

    @property
    def total_used(self) -> int:
        return self.bot_used + self.world_used + self.memory_used

    @property
    def reserved_total(self) -> int:
        return self.reserved_bot + self.reserved_world + self.reserved_memory

    def remaining(self, ledger: str | None = None) -> int | None:
        total_remaining = (
            None
            if self.total_limit is None
            else max(0, self.total_limit - self.total_used - self.reserved_total)
        )
        if ledger is None:
            return total_remaining
        fields = {
            "bot": (self.bot_used, self.bot_limit, self.reserved_bot),
            "world": (self.world_used, self.world_limit, self.reserved_world),
            "memory": (self.memory_used, self.memory_limit, self.reserved_memory),
        }
        if ledger not in fields:
            raise ValueError(f"unknown quota ledger: {ledger}")
        used, limit, reserved = fields[ledger]
        ledger_remaining = None if limit is None else max(0, limit - used - reserved)
        available = [item for item in (total_remaining, ledger_remaining) if item is not None]
        return min(available) if available else None

    def exhausted(self) -> bool:
        limits = (
            (self.bot_used, self.bot_limit),
            (self.world_used, self.world_limit),
            (self.memory_used, self.memory_limit),
            (self.total_used, self.total_limit),
        )
        return any(limit is not None and used >= limit for used, limit in limits)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self) | {
            "total_used": self.total_used,
            "reserved_total": self.reserved_total,
            "remaining_total": self.remaining(),
            "remaining_bot": self.remaining("bot"),
            "remaining_world": self.remaining("world"),
            "remaining_memory": self.remaining("memory"),
            "exhausted": self.exhausted(),
        }


def dataclass_dict(value: Any) -> dict[str, Any]:
    """把枚举转换成 JSON 可编码的字典。"""

    if is_dataclass(value) and not isinstance(value, type):
        raw = {item.name: getattr(value, item.name) for item in fields(value) if not item.metadata.get("private")}
    else:
        raw = asdict(value)
    return _json_value(raw)


def _json_value(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value
