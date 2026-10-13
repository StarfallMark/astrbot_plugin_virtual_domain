"""客户端、服务端与 AstrBot 连接器共用的 JSON 协议骨架。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


PROTOCOL_VERSION = 1


class ProtocolError(ValueError):
    pass


@dataclass(slots=True)
class Envelope:
    message_type: str
    payload: dict[str, Any]
    request_id: str = ""
    world_id: str | None = None
    state_version: int | None = None
    event_sequence: int | None = None
    protocol_version: int = PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = str(uuid.uuid4())
        if not self.message_type:
            raise ProtocolError("消息类型不能为空")
        if self.state_version is not None and self.state_version < 0:
            raise ProtocolError("状态版本不能为负数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_type": self.message_type,
            "request_id": self.request_id,
            "world_id": self.world_id,
            "state_version": self.state_version,
            "event_sequence": self.event_sequence,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Envelope":
        if not isinstance(value, dict):
            raise ProtocolError("协议消息必须是 JSON 对象")
        if int(value.get("protocol_version", 0)) != PROTOCOL_VERSION:
            raise ProtocolError("不支持的协议版本")
        payload = value.get("payload", {})
        if not isinstance(payload, dict):
            raise ProtocolError("payload 必须是对象")
        return cls(
            str(value.get("message_type", "")),
            payload,
            str(value.get("request_id", "")),
            value.get("world_id"),
            value.get("state_version"),
            value.get("event_sequence"),
            int(value.get("protocol_version", PROTOCOL_VERSION)),
        )
