"""Native AstrBot pipeline event with an output confined to the game."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.core.platform import AstrBotMessage, MessageMember, MessageType, PlatformMetadata

from .usage import extract_actual_token_count


class VirtualGameEvent(AstrMessageEvent):
    def __init__(
        self,
        prompt: str,
        *,
        account_id: str,
        world_id: str,
        purpose: str,
        display_name: str,
        conversation_origin: str = "",
        conversation_platform_metadata: PlatformMetadata | None = None,
    ) -> None:
        message = AstrBotMessage()
        message.type = MessageType.FRIEND_MESSAGE
        # Bot decisions never write into a player's ordinary chat conversation.
        identity = account_id if purpose == "chat" else "bot-player"
        session_purpose = "chat" if purpose == "chat" else "action"
        isolated_identity = f"virtual-domain!{identity}!{world_id}"
        isolated_session = f"{isolated_identity}!{session_purpose}"
        origin_parts = conversation_origin.strip().split(":", 2)
        bound_session = None
        if (
            purpose == "chat"
            and len(origin_parts) == 3
            and origin_parts[0].strip()
            and origin_parts[1] == MessageType.FRIEND_MESSAGE.value
            and origin_parts[2].strip().isdecimal()
        ):
            bound_session = (origin_parts[0], origin_parts[2])
        route_to_daily_session = bound_session is not None
        self.bot_id = "virtual-domain" if route_to_daily_session else f"virtual-domain!{world_id}"
        message.self_id = self.bot_id
        platform_id, qq_id = bound_session or ("", "")
        if route_to_daily_session:
            # Reconstruct AstrBot's MessageSession fields instead of using the
            # whole unified origin as session_id. AstrBot adds platform/type
            # itself, so passing the full origin here creates a nested
            # webchat:FriendMessage:<qq-origin> session.
            message.session_id = qq_id
            message.sender = MessageMember(qq_id, display_name)
            metadata = conversation_platform_metadata
            if metadata is None or metadata.id != platform_id:
                metadata = PlatformMetadata(platform_id, "虚拟域游戏聊天", platform_id)
            session_id = qq_id
        else:
            message.session_id = isolated_session
            message.sender = MessageMember(
                isolated_identity,
                display_name if purpose == "chat" else "Bot 玩家",
            )
            metadata = PlatformMetadata("webchat", "虚拟域", "webchat")
            session_id = isolated_session
        message.message_id = str(uuid.uuid4())
        message.message = [Plain(prompt)]
        message.message_str = prompt
        message.raw_message = {"source": "virtual_domain", "purpose": purpose}
        super().__init__(prompt, message, metadata, session_id)
        self.is_wake = True
        self.is_at_or_wake_command = True
        self.output_parts: list[str] = []
        self.done = asyncio.Event()
        self.actual_tokens: int | None = None
        self.response_token_total = 0
        self.trace_token_total = 0
        self.final_text = ""
        self.request_prompt_chars = 0
        self.set_extra("virtual_domain_event", True)
        self.set_extra("virtual_domain_bound_daily_session", route_to_daily_session)
        self.set_extra(
            "virtual_domain_original_conversation_origin",
            conversation_origin.strip() if route_to_daily_session else "",
        )
        original_record = self.trace.record

        def record(action, **fields):
            if action == "astr_agent_complete":
                stats = fields.get("stats")
                usage = (
                    stats.get("token_usage")
                    if isinstance(stats, Mapping)
                    else getattr(stats, "token_usage", None)
                )
                self.capture_trace_usage(usage)
                response_text = str(fields.get("resp") or "")
                if not response_text and self.reply_text:
                    self.set_extra("virtual_domain_provider_failed", True)
            original_record(action, **fields)

        # Capture actual aggregate tool-loop usage without changing core code.
        self.trace.record = record

    def get_self_id(self) -> str:
        return self.bot_id

    def capture_response_usage(self, usage) -> None:
        """Accumulate actual usage reported for an individual LLM response."""

        self.response_token_total += extract_actual_token_count(usage)
        self._update_actual_tokens()

    def capture_trace_usage(self, usage) -> None:
        """Capture AstrBot's aggregate agent usage when its trace provides it."""

        self.trace_token_total = max(
            self.trace_token_total,
            extract_actual_token_count(usage),
        )
        self._update_actual_tokens()

    def _update_actual_tokens(self) -> None:
        total = max(self.response_token_total, self.trace_token_total)
        self.actual_tokens = total or None

    async def send(self, message) -> None:
        if message is not None:
            text = message.get_plain_text()
            if text:
                self.output_parts.append(text)
            self._has_send_oper = True

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        async for chain in generator:
            if chain is None or getattr(chain, "type", "") in {"reasoning", "tool_call", "tool_call_result"}:
                continue
            text = chain.get_plain_text()
            if text:
                self.output_parts.append(text)
        self._has_send_oper = True

    def cleanup_temporary_local_files(self) -> None:
        # Scheduler invokes cleanup only after response hooks and history saving.
        try:
            super().cleanup_temporary_local_files()
        finally:
            self.done.set()

    @property
    def reply_text(self) -> str:
        return "".join(self.output_parts).strip()
