"""虚拟域 AstrBot 连接器。

插件通过独立服务端的认证桥接，把虚拟域 Bot 请求送入 AstrBot 正常事件队列，保留当前人格、工具与已启用插件，并把游戏回复限制在虚拟域客户端。`/vd` 提供连接状态与世界摘要；行动和游戏内聊天在独立客户端进行，未绑定 QQ 玩家账号前不从 QQ 伪造玩家身份。
"""

from __future__ import annotations

import asyncio
import shlex
import unicodedata
import uuid

try:
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, filter
    from astrbot.api.star import Context, Star, register
except ImportError:  # 允许在没有 AstrBot 主程序时运行核心测试
    AstrBotConfig = dict  # type: ignore[assignment,misc]

    class _Logger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

    logger = _Logger()

    class Context:  # type: ignore[no-redef]
        pass

    class Star:  # type: ignore[no-redef]
        def __init__(self, context=None, config=None):
            self.context, self.config = context, config or {}

    class _Filter:
        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

        on_llm_request = command
        on_llm_response = command

    filter = _Filter()

    def register(*args, **kwargs):
        return lambda cls: cls

try:
    # AstrBot 会以 data.plugins.<plugin_name>.main 的方式加载插件，使用
    # 相对导入才能在插件包内部找到连接器；直接从插件目录运行时再回退
    # 到同目录导入，方便独立做加载检查。
    from .virtual_domain_plugin.connector.http_client import ConnectorError, VirtualDomainClient
    from .virtual_domain_plugin.connector.usage import extract_actual_token_count
except ImportError:
    from virtual_domain_plugin.connector.http_client import ConnectorError, VirtualDomainClient
    from virtual_domain_plugin.connector.usage import extract_actual_token_count


def _contains_displayable_text(value: object) -> bool:
    """Reject model replies that contain only whitespace or punctuation."""
    return any(
        unicodedata.category(character)[0] in {"L", "N", "S"}
        for character in str(value or "")
    )


class AstrBotEventTimeout(RuntimeError):
    def __init__(self, message: str, actual_tokens: int | None = None):
        super().__init__(message)
        self.actual_tokens = actual_tokens


@register(
    "astrbot_plugin_virtual_domain",
    "虚拟域项目组",
    "连接自托管虚拟域服务端，让 AstrBot 以满血 Bot 身份参与世界游戏",
    "0.1.0",
)
class VirtualDomainPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | dict | None = None):
        try:
            super().__init__(context, config)
        except TypeError:
            super().__init__(context)
        self.config = config or {}
        self.client = VirtualDomainClient(
            self._get("server_url", "http://127.0.0.1:8787"),
            self._get("connector_token", ""),
            tls_ca_file=self._get("tls_ca_file", ""),
            tls_client_cert_file=self._get("tls_client_cert_file", ""),
            tls_client_key_file=self._get("tls_client_key_file", ""),
        )
        self.default_world_id = self._get("world_id", "")
        self.admin_qq = str(self._get("admin_qq", "") or "").strip()
        self._bridge_task: asyncio.Task | None = None
        self._bridge_requests: set[asyncio.Task] = set()
        self._bridge_connection = None

    def _get(self, key: str, default):
        getter = getattr(self.config, "get", None)
        return getter(key, default) if getter else default

    @filter.on_llm_request(priority=-1000000)
    async def _inject_virtual_domain_prompt(self, event, req, *args, **kwargs):
        system = event.get_extra("virtual_domain_system_prompt")
        if system:
            current = getattr(req, "system_prompt", "") or ""
            req.system_prompt = f"{current}\n{system}".strip()
            event.request_prompt_chars = len(req.system_prompt)

    @filter.on_llm_response(priority=200000)
    async def _capture_virtual_domain_usage(self, event, resp, *args, **kwargs):
        if event.get_extra("virtual_domain_system_prompt"):
            try:
                event.set_extra("virtual_domain_usage_response_received", True)
            except Exception:
                event.virtual_domain_usage_response_received = True
            usage = getattr(resp, "usage", None)
            capture = getattr(event, "capture_response_usage", None)
            if callable(capture):
                capture(usage)
            else:
                count = extract_actual_token_count(usage)
                if count:
                    event.actual_tokens = max(int(event.actual_tokens or 0), count)
            event.final_text = str(getattr(resp, "completion_text", "") or "")

    async def initialize(self):
        if not self.client.endpoint:
            logger.warning("[虚拟域] 未配置服务端地址，连接器保持待机")
            return
        self._bridge_task = asyncio.create_task(self._bridge_loop())
        try:
            await asyncio.to_thread(self.client.health)
            logger.info("[虚拟域] 已连接服务端 %s", self.client.endpoint)
        except ConnectorError as exc:
            logger.warning("[虚拟域] 服务端暂不可用：%s", exc)

    async def _bridge_loop(self) -> None:
        """Maintain the authenticated persistent bridge to the world service."""
        while True:
            connection = None
            receive_task = None
            try:
                connection = await asyncio.to_thread(
                    self.client.bridge_connect,
                    default_world_id=self.default_world_id,
                )
                self._bridge_connection = connection
                logger.info("[虚拟域] WebSocket 桥已连接：%s", self.client.endpoint)
                receive_task = asyncio.create_task(asyncio.to_thread(connection.receive_json))
                while True:
                    done, _ = await asyncio.wait({receive_task}, timeout=15)
                    if not done:
                        await asyncio.to_thread(connection.send_ping)
                        continue
                    message = receive_task.result()
                    receive_task = asyncio.create_task(asyncio.to_thread(connection.receive_json))
                    if message.get("type") == "request":
                        task = asyncio.create_task(self._handle_bridge_request(message, connection))
                        self._bridge_requests.add(task)
                        task.add_done_callback(self._bridge_requests.discard)
                    elif message.get("type") == "pong":
                        continue
                    else:
                        logger.warning("[虚拟域] 收到未知桥接消息类型：%s", message.get("type"))
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.warning("[虚拟域] Bot 接入请求处理失败：%s", exc)
            finally:
                if self._bridge_connection is connection:
                    self._bridge_connection = None
                if connection is not None:
                    await asyncio.to_thread(connection.close)
                if receive_task is not None:
                    await asyncio.gather(receive_task, return_exceptions=True)
            await asyncio.sleep(2)

    async def _handle_bridge_request(self, request: dict, connection=None) -> None:
        try:
            response = await self._generate_bridge_response(request)
        except Exception as exc:
            logger.warning(
                "[虚拟域] AstrBot 处理失败：purpose=%s error_type=%s",
                request.get("purpose", "chat"),
                type(exc).__name__,
            )
            error_text = (
                "AstrBot 事件处理超时"
                if isinstance(exc, AstrBotEventTimeout)
                else f"AstrBot 生成失败：{type(exc).__name__}"
            )
            response = {"error": error_text}
            actual_tokens = getattr(exc, "actual_tokens", None)
            if isinstance(actual_tokens, int) and not isinstance(actual_tokens, bool) and actual_tokens > 0:
                response["actual_tokens"] = actual_tokens
        try:
            if connection is None:
                await asyncio.to_thread(self.client.bridge_respond, request.get("request_id", ""), response)
            else:
                await asyncio.to_thread(
                    connection.send_json,
                    {"type": "response", "request_id": request.get("request_id", ""), "response": response},
                )
        except Exception as exc:
            logger.warning("[虚拟域] 回传请求结果失败：%s", type(exc).__name__)

    async def _generate_bridge_response(self, request: dict) -> dict:
        purpose = request.get("purpose", "chat")
        system = (
            "你正在参与虚拟域世界游戏。你是与用户平等的 Bot 玩家，世界模拟器负责裁定结果，"
            "不能自行宣布行动成功。保持 AstrBot 当前生效的人设和已启用插件约束。\n"
            f"游戏内浅层设定：{request.get('shallow_worldview', '')}\n"
            f"当前可见经历：{request.get('visible_context', '')}\n"
        )
        if purpose == "action":
            system += "请只输出你想采取的一句行动。"
        elif purpose == "interaction_response":
            system += "你正在回应另一位玩家对你的行动或请求。按你的人设和关系决定说什么或做什么，只输出你的回应内容，不裁定世界结果，也不替对方行动。"
        elif purpose == "autonomous_action":
            system += "用户离线。决定是否独自游玩，想行动时只输出一句你自己的低风险行动，不控制离线用户，不造成危险或长期后果；不想行动时只输出 <VD_PASS>。"
        else:
            system += (
                "这是游戏内聊天，不推进世界状态。根据当前对话和已提供的世界经历直接回复；"
                "除非用户明确要求查询外部资料，否则不要调用知识库或其他工具。"
            )
        if request.get("role_mode") == "character":
            system += "你操控绑定角色；聊天时区分你自己与角色，行动只控制该绑定角色。"
        return await self._run_full_astrbot_event(request, system)

    async def _run_full_astrbot_event(self, request: dict, system: str) -> dict:
        """把虚拟域请求放入 AstrBot 正常事件队列，保留事件钩子和插件链路。"""
        try:
            from .virtual_domain_plugin.connector.game_event import VirtualGameEvent
        except ImportError:
            from virtual_domain_plugin.connector.game_event import VirtualGameEvent

        conversation_origin = str(request.get("conversation_origin", "")).strip()
        event = VirtualGameEvent(
            str(request.get("content", "")),
            account_id=str(request.get("account_id", "bot")),
            world_id=str(request.get("world_id", "world")),
            purpose=str(request.get("purpose", "chat")),
            display_name=str(request.get("display_name", "玩家")),
            conversation_origin=conversation_origin,
            conversation_platform_metadata=self._conversation_platform_metadata(conversation_origin),
        )
        event.set_extra("virtual_domain_system_prompt", system)
        await self.context.get_event_queue().put(event)
        try:
            await asyncio.wait_for(event.done.wait(), timeout=180)
        except asyncio.TimeoutError as exc:
            try:
                from astrbot.core.utils.active_event_registry import active_event_registry

                active_event_registry.request_agent_stop_all(event.unified_msg_origin)
            except Exception:
                event.stop_event()
            try:
                await asyncio.wait_for(event.done.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
            raise AstrBotEventTimeout(
                "AstrBot 事件处理超时",
                actual_tokens=event.actual_tokens,
            ) from exc
        result = {
            "text": event.reply_text,
            "actual_tokens": event.actual_tokens,
            "usage_response_received": bool(
                event.get_extra("virtual_domain_usage_response_received")
                or getattr(event, "virtual_domain_usage_response_received", False)
            ),
            "pipeline": "astrbot-event",
            "prompt_chars": event.request_prompt_chars,
        }
        if event.get_extra("virtual_domain_provider_failed"):
            result["error"] = "AstrBot 上游模型调用失败，请管理员检查 AstrBot 日志"
            error_kind = "provider_failed"
        elif not event.actual_tokens:
            result["error"] = (
                "AstrBot 上游已响应但未返回实际 Token 用量"
                if result["usage_response_received"]
                else "AstrBot 未返回实际 Token 用量，或事件被插件/白名单拦截"
            )
            error_kind = "usage_unavailable"
        elif not _contains_displayable_text(event.reply_text):
            result["error"] = "AstrBot 回复没有可显示正文，可能是上游模型输出异常"
            error_kind = "empty_output"
        else:
            error_kind = ""
        if error_kind:
            logger.warning(
                "[虚拟域] 正常事件链未返回可用回复：purpose=%s error_kind=%s actual_tokens=%s",
                request.get("purpose", "chat"),
                error_kind,
                event.actual_tokens,
            )
        logger.info("[虚拟域] 正常事件链完成：purpose=%s text_chars=%s actual_tokens=%s", request.get("purpose"), len(event.reply_text), event.actual_tokens)
        return result

    def _conversation_platform_metadata(self, conversation_origin: str):
        """Return the matching platform metadata for a bound AstrBot session."""
        parts = conversation_origin.split(":", 2)
        if len(parts) != 3 or not parts[0]:
            return None
        platform_id = parts[0]
        manager = getattr(self.context, "platform_manager", None)
        if manager is None:
            return None
        getter = getattr(manager, "get_insts", None)
        instances = getter() if callable(getter) else getattr(manager, "platform_insts", [])
        for instance in instances or []:
            meta_getter = getattr(instance, "meta", None)
            if not callable(meta_getter):
                continue
            try:
                metadata = meta_getter()
            except Exception:
                continue
            if getattr(metadata, "id", None) == platform_id:
                return metadata
        return None

    def _arg_text(self, event: AstrMessageEvent) -> str:
        return (getattr(event, "message_str", "") or "").strip()

    def _world_id(self) -> str:
        return self.client.current_world()["id"]

    def _qq_sender(self, event: AstrMessageEvent) -> str:
        platform_getter = getattr(event, "get_platform_name", None)
        platform = str(platform_getter() if callable(platform_getter) else "").lower()
        private_getter = getattr(event, "is_private_chat", None)
        is_private = bool(private_getter()) if callable(private_getter) else False
        sender_getter = getattr(event, "get_sender_id", None)
        sender_id = str(sender_getter() if callable(sender_getter) else "").strip()
        if platform not in {"aiocqhttp", "qq", "onebot", "napcat"} or not is_private or not sender_id.isdecimal():
            raise ConnectorError("该指令只接受 QQ 私聊消息")
        return sender_id

    @filter.command("vd")
    async def virtual_domain_command(self, event: AstrMessageEvent):
        """虚拟域连接器指令：状态、世界、绑定、暂停、切换和备份。"""
        try:
            tokens = shlex.split(self._arg_text(event))
        except ValueError:
            tokens = []
        if tokens and tokens[0].lower() in {"/vd", "vd"}:
            tokens = tokens[1:]
        subcommand = tokens[0].lower() if tokens else "help"
        try:
            if subcommand in {"help", "帮助"}:
                yield event.plain_result(
                    "/vd status：检查连接\n"
                    "/vd worlds：查看世界摘要\n"
                    "/vd invite [有效小时数]：管理员生成一次性加入邀请\n"
                    "/vd invite revoke <邀请码>：管理员撤销未使用的邀请码\n"
                    "/vd summary：管理员查看当前世界运行、交互和额度摘要\n"
                    "/vd interactions：管理员查看待回应和失败交互\n"
                    "/vd audit [条数]：管理员查看最近操作记录\n"
                    "/vd quota：查看当前世界 Token 账本（管理员私聊）\n"
                    "/vd bind <绑定码>：在 QQ 私聊中绑定当前账号\n"
                    "/vd nodes <世界ID> [before_sequence]：管理员分页查看世界节点\n"
                    "/vd rollback <世界ID> <节点ID> [confirm]：管理员回滚到指定节点\n"
                    "/vd pause [世界ID|current]：管理员暂停世界\n"
                    "/vd resume [世界ID|current]：管理员恢复世界\n"
                    "/vd switch <世界ID> [confirm]：切换当前世界\n"
                    "/vd delete <世界ID>：管理员永久删除世界（需再次确认名称）\n"
                    "/vd backup：管理员创建手动备份\n"
                    "/vd backups：查看最近备份\n"
                    "行动和游戏内聊天请使用虚拟域桌面客户端。"
                )
                return
            if subcommand in {"status", "状态"}:
                result = await asyncio.to_thread(self.client.health)
                yield event.plain_result(f"虚拟域服务端正常：{result.get('version', 'unknown')}")
                return
            if subcommand in {"worlds", "世界"}:
                result = await asyncio.to_thread(self.client.worlds)
                worlds = result.get("worlds", [])
                yield event.plain_result(
                    "当前没有世界"
                    if not worlds
                    else "\n".join(
                        f"{item['name']} · {item['id']} · {item['mode']} · {item['status']}"
                        + (" · 当前运行" if item.get("current") else "")
                        for item in worlds
                    )
                )
                return
            if subcommand in {"invite", "invitation", "邀请"}:
                if len(tokens) >= 2 and tokens[1].lower() in {"revoke", "撤销"}:
                    if len(tokens) != 3:
                        yield event.plain_result("用法：/vd invite revoke <未使用的邀请码>")
                        return
                    sender_id = self._qq_sender(event)
                    if not self.admin_qq:
                        yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                        return
                    worlds = await asyncio.to_thread(self.client.worlds)
                    current = next((item for item in worlds.get("worlds", []) if item.get("current")), None)
                    if current is None:
                        yield event.plain_result("当前没有运行中的世界，无法管理加入邀请。")
                        return
                    result = await asyncio.to_thread(
                        self.client.admin_revoke_invitation,
                        str(current["id"]),
                        tokens[2],
                        sender_qq_id=sender_id,
                        configured_admin_qq=self.admin_qq,
                    )
                    yield event.plain_result(
                        f"世界「{current.get('name', current['id'])}」中的未使用邀请码已撤销。"
                    )
                    return
                if len(tokens) > 2:
                    yield event.plain_result("用法：/vd invite [有效小时数] 或 /vd invite revoke <邀请码>")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                hours = 24
                if len(tokens) == 2:
                    try:
                        hours = int(tokens[1])
                    except ValueError:
                        yield event.plain_result("有效期必须是 1 到 720 之间的整数小时。")
                        return
                    if not 1 <= hours <= 24 * 30:
                        yield event.plain_result("有效期必须是 1 到 720 之间的整数小时。")
                        return
                worlds = await asyncio.to_thread(self.client.worlds)
                current = next((item for item in worlds.get("worlds", []) if item.get("current")), None)
                if current is None:
                    yield event.plain_result("当前没有运行中的世界，无法创建加入邀请。")
                    return
                invitation = await asyncio.to_thread(
                    self.client.admin_create_invitation,
                    str(current["id"]),
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                    hours=hours,
                )
                yield event.plain_result(
                    f"世界「{current.get('name', current['id'])}」的一次性加入邀请码：\n"
                    f"{invitation['code']}\n"
                    f"有效期至：{invitation['expires_at']}\n"
                    "邀请码只提供进入资格；玩家仍需使用你另行提供的服务端地址及连接方式加入。"
                )
                return
            if subcommand in {"interactions", "交互", "待回应"}:
                if len(tokens) != 1:
                    yield event.plain_result("用法：/vd interactions")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                result = await asyncio.to_thread(
                    self.client.admin_pending_interactions,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )
                world = result.get("current_world")
                if not world:
                    yield event.plain_result("当前没有运行中的世界。")
                    return
                interactions = result.get("interactions", [])
                if not interactions:
                    yield event.plain_result(f"世界「{world.get('name', world['id'])}」没有待回应或失败的交互。")
                    return
                status_labels = {"pending": "等待回应", "ready": "等待结算", "failed": "结算失败"}
                lines = []
                for item in interactions[:5]:
                    status = str(item.get("status", "unknown"))
                    attempts = int(item.get("resolution_attempts", 0))
                    action = str(item.get("action", "")).replace("\n", " ").strip()
                    prompt = str(item.get("prompt", "")).replace("\n", " ").strip()
                    if len(action) > 160:
                        action = action[:159] + "…"
                    if len(prompt) > 120:
                        prompt = prompt[:119] + "…"
                    wait_status = (
                        f"剩余：{int(item.get('seconds_remaining', 0))} 秒"
                        if status == "pending"
                        else "正在结算"
                        if status == "ready"
                        else "需要管理员处理"
                    )
                    attempt_label = f" · 已结算尝试 {attempts}/3" if attempts else ""
                    detail = (
                        f"{status_labels.get(status, status)} · 目标：{item.get('target_name', '未知角色')} · "
                        f"{wait_status}{attempt_label}\n"
                        f"行动：{action or '（空）'}\n回应要求：{prompt or '（无）'}"
                    )
                    if item.get("response_text"):
                        response = str(item["response_text"]).replace("\n", " ").strip()
                        if len(response) > 120:
                            response = response[:119] + "…"
                        detail += f"\n已收到回应：{response}"
                    lines.append(detail)
                suffix = f"\n仅显示前 5 项，共 {len(interactions)} 项。" if len(interactions) > 5 else ""
                yield event.plain_result(f"世界「{world.get('name', world['id'])}」的待处理交互：\n" + "\n\n".join(lines) + suffix)
                return
            if subcommand in {"audit", "审计", "日志"}:
                if len(tokens) > 2:
                    yield event.plain_result("用法：/vd audit [条数，1 到 20]")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                limit = 10
                if len(tokens) == 2:
                    try:
                        limit = int(tokens[1])
                    except ValueError:
                        yield event.plain_result("条数必须是 1 到 20 之间的整数。")
                        return
                    if not 1 <= limit <= 20:
                        yield event.plain_result("条数必须是 1 到 20 之间的整数。")
                        return
                result = await asyncio.to_thread(
                    self.client.admin_audit_summary,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                    limit=limit,
                )
                entries = result.get("entries", [])
                if not entries:
                    yield event.plain_result("暂无管理员操作记录。")
                    return

                def short(value: object, maximum: int = 48) -> str:
                    text = str(value or "").replace("\n", " ").strip()
                    return text if len(text) <= maximum else text[: maximum - 1] + "…"

                result_labels = {
                    "success": "成功",
                    "failed": "失败",
                    "retry": "重试",
                    "pending": "处理中",
                }
                lines = [
                    f"{short(item.get('created_at'), 32)} · "
                    f"{short(item.get('action'), 40)} · "
                    f"{result_labels.get(str(item.get('result', '')), short(item.get('result'), 12))} · "
                    f"{short(item.get('target'), 40)}"
                    for item in entries[:limit]
                ]
                yield event.plain_result("最近管理员操作记录（不含详情）：\n" + "\n".join(lines))
                return
            if subcommand in {"summary", "摘要", "current", "当前"}:
                if len(tokens) != 1:
                    yield event.plain_result("用法：/vd summary")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                summary = await asyncio.to_thread(
                    self.client.admin_world_summary,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )
                current = summary.get("current_world")
                if not current:
                    yield event.plain_result(
                        f"当前没有运行中的世界。已保存世界：{int(summary.get('world_count', 0))} 个。"
                    )
                    return
                quota = summary.get("quota") or {}
                total_limit = quota.get("total_limit")
                total_ceiling = "未设置" if total_limit is None else f"{int(total_limit):,}"
                used = int(quota.get("total_used", 0))
                interactions = summary.get("interactions") or {}
                pending = int(interactions.get("pending", 0))
                ready = int(interactions.get("ready", 0))
                failed = int(interactions.get("failed", 0))
                bot_daily = summary.get("bot_daily") or {}
                yield event.plain_result(
                    f"当前世界：{current.get('name', current['id'])}（{current['id']}）\n"
                    f"状态：{current.get('status', 'unknown')} · "
                    f"模式：{current.get('mode', 'unknown')} · "
                    f"版本：{int(current.get('state_version', 0))}\n"
                    f"在线玩家：{int(summary.get('players_online', 0))} · "
                    f"Bot 请求：{'运行中' if summary.get('bot_request_running') else '空闲'}\n"
                    f"待回应交互：{pending} · 待结算：{ready} · 失败：{failed}\n"
                    f"Token 总计：{used:,}/{total_ceiling} · "
                    f"今日 Bot 行动：{int(bot_daily.get('bot_actions', 0))}"
                )
                return
            if subcommand in {"quota", "额度", "用量"}:
                if len(tokens) != 1:
                    yield event.plain_result("用法：/vd quota")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                worlds = await asyncio.to_thread(self.client.worlds)
                current = next((item for item in worlds.get("worlds", []) if item.get("current")), None)
                if current is None:
                    yield event.plain_result("当前没有运行中的世界。")
                    return
                quota = await asyncio.to_thread(
                    self.client.admin_quota_snapshot,
                    str(current["id"]),
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )

                def ledger(name: str, used_key: str, limit_key: str) -> str:
                    limit = quota.get(limit_key)
                    ceiling = "未设置" if limit is None else f"{int(limit):,}"
                    return f"{name} {int(quota.get(used_key, 0)):,}/{ceiling}"

                total_limit = quota.get("total_limit")
                total_ceiling = "未设置" if total_limit is None else f"{int(total_limit):,}"
                remaining_total = quota.get("remaining_total")
                remaining_label = "未设置" if remaining_total is None else f"{int(remaining_total):,}"
                reserved_total = int(quota.get("reserved_total", 0))
                usage_role_names = {
                    "bot": "Bot",
                    "world": "世界模型",
                    "embedding": "记忆嵌入模型",
                    "rerank": "记忆重排模型",
                }
                usage_support = quota.get("usage_support") or {}
                missing_usage = [
                    usage_role_names[role]
                    for role, state in usage_support.items()
                    if role in usage_role_names and state == "missing"
                ]
                usage_warning = (
                    f"\n统计缺口：{'、'.join(missing_usage)}未返回实际 Token 用量，请检查供应商。"
                    if missing_usage
                    else ""
                )
                usage_scope_warning = (
                    "\n统计范围提示：AstrBot 或第三方插件自行发起的模型调用，"
                    "以及未返回给连接器的额外用量，可能不在此账本中；"
                    "Bot 额度不构成这类调用的完整硬上限。"
                )
                yield event.plain_result(
                    f"世界：{current.get('name', current['id'])}\n"
                    f"实际已用 {int(quota.get('total_used', 0)):,}/{total_ceiling} Token · "
                    f"共享剩余 {remaining_label} · 在途预留约 {reserved_total:,}\n"
                    f"{ledger('Bot', 'bot_used', 'bot_limit')} · "
                    f"{ledger('世界', 'world_used', 'world_limit')} · "
                    f"{ledger('记忆', 'memory_used', 'memory_limit')}\n"
                    + ("额度已耗尽，推演已停止。" if quota.get("exhausted") else "额度尚未耗尽。")
                    + usage_warning
                    + usage_scope_warning
                )
                return
            if subcommand in {"nodes", "节点", "存档节点"}:
                if len(tokens) not in {2, 3}:
                    yield event.plain_result("用法：/vd nodes <世界ID> [before_sequence]")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                before_sequence = None
                if len(tokens) == 3:
                    try:
                        before_sequence = int(tokens[2])
                    except ValueError:
                        yield event.plain_result("before_sequence 必须是非负整数。")
                        return
                    if before_sequence < 0:
                        yield event.plain_result("before_sequence 必须是非负整数。")
                        return
                result = await asyncio.to_thread(
                    self.client.admin_world_nodes,
                    tokens[1],
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                    before_sequence=before_sequence,
                    limit=10,
                )
                world = result.get("world") or {"id": tokens[1], "name": tokens[1]}
                nodes = result.get("nodes", [])
                if not nodes:
                    yield event.plain_result(f"世界「{world.get('name', world['id'])}」没有可显示的存档节点。")
                    return
                kind_labels = {"action": "行动", "time": "时间", "system": "系统", "chat": "聊天"}
                lines = []
                for node in nodes:
                    summary = str(node.get("summary", "世界初始状态")).replace("\n", " ").strip()
                    if len(summary) > 100:
                        summary = summary[:99] + "…"
                    kind = kind_labels.get(str(node.get("event_kind") or ""), "初始/维护")
                    lines.append(
                        f"序号 {int(node.get('sequence', 0))} · 状态版本 {int(node.get('state_version', 0))} · "
                        f"{kind} · {node.get('created_at', '')}\n"
                        f"{summary or '世界初始状态'}\n节点ID：{node.get('node_id', '')}"
                    )
                next_cursor = result.get("next_before_sequence")
                suffix = (
                    f"\n更早节点：/vd nodes {world['id']} {int(next_cursor)}"
                    if next_cursor is not None
                    else "\n已到最早节点。"
                )
                yield event.plain_result(
                    f"世界「{world.get('name', world['id'])}」的存档节点（最新在前）：\n"
                    + "\n\n".join(lines)
                    + suffix
                )
                return
            if subcommand in {"rollback", "回滚"}:
                if len(tokens) not in {3, 4} or (
                    len(tokens) == 4 and tokens[3].lower() not in {"confirm", "确认"}
                ):
                    yield event.plain_result("用法：/vd rollback <世界ID> <节点ID> [confirm]")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                if len(tokens) == 3:
                    yield event.plain_result(
                        f"即将把世界 {tokens[1]} 回滚到节点 {tokens[2]}。"
                        "该节点之后的剧情、节点、相关记忆和待回应交互会被丢弃，已产生的 Token 用量保留。"
                        f"\n确认请再次发送：/vd rollback {tokens[1]} {tokens[2]} confirm"
                    )
                    return
                result = await asyncio.to_thread(
                    self.client.admin_rollback_world,
                    tokens[1],
                    tokens[2],
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )
                yield event.plain_result(
                    f"世界 {result.get('world_id', tokens[1])} 已回滚到序号 "
                    f"{int(result.get('target_sequence', 0))} 的节点。"
                    f"丢弃后续事件 {int(result.get('discarded_event_count', 0))} 条，"
                    f"取消待回应交互 {int(result.get('cancelled_interaction_count', 0))} 条；"
                    "已产生的 Token 用量保留。"
                )
                return
            if subcommand in {"backup", "备份"}:
                if len(tokens) != 1:
                    yield event.plain_result("用法：/vd backup")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                result = await asyncio.to_thread(
                    self.client.admin_create_backup,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )
                yield event.plain_result(
                    f"服务端备份已创建：{result['id']}（{result['size_bytes']} 字节）。备份文件可在桌面客户端中下载。"
                )
                return
            if subcommand in {"backups", "备份列表"}:
                if len(tokens) != 1:
                    yield event.plain_result("用法：/vd backups")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                backups = await asyncio.to_thread(
                    self.client.admin_list_backups,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )
                items = backups.get("backups", [])[:5]
                yield event.plain_result(
                    "暂无服务端备份。"
                    if not items
                    else "最近的服务端备份：\n" + "\n".join(
                        f"{item['created_at']} · {item['kind']} · {item['id']} · {item['size_bytes']} 字节"
                        for item in items
                    )
                )
                return
            if subcommand in {"bind", "绑定"}:
                if len(tokens) != 2:
                    yield event.plain_result("用法：/vd bind <桌面客户端生成的绑定码>")
                    return
                sender_id = self._qq_sender(event)
                conversation_origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
                await asyncio.to_thread(
                    self.client.bind_qq,
                    sender_id,
                    tokens[1],
                    session_origin=conversation_origin,
                )
                yield event.plain_result("QQ 身份绑定成功；其他玩家看不到你的 QQ 号。")
                return
            if subcommand in {"switch", "切换"}:
                if len(tokens) not in {2, 3} or (len(tokens) == 3 and tokens[2].lower() not in {"confirm", "确认"}):
                    yield event.plain_result("用法：/vd switch <世界ID> [confirm]")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                confirmed = len(tokens) == 3
                try:
                    result = await asyncio.to_thread(
                        self.client.admin_switch_world,
                        tokens[1],
                        sender_qq_id=sender_id,
                        configured_admin_qq=self.admin_qq,
                        confirm_online_disconnect=confirmed,
                    )
                except ConnectorError as exc:
                    message = str(exc)
                    if message.startswith("切换确认：") and not confirmed:
                        yield event.plain_result(message + f"\n如确实要切换，请再次发送：/vd switch {tokens[1]} confirm")
                        return
                    raise
                yield event.plain_result(f"当前世界已切换为 {result.get('world_id', tokens[1])}；旧世界玩家会在客户端收到切换提示并断开。")
                return
            if subcommand in {"delete", "删除"}:
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                if len(tokens) == 2:
                    worlds = await asyncio.to_thread(self.client.worlds)
                    world = next(
                        (item for item in worlds.get("worlds", []) if item.get("id") == tokens[1]),
                        None,
                    )
                    if world is None:
                        yield event.plain_result("没有找到该世界；请先使用 /vd worlds 查看可管理的世界。")
                        return
                    confirmation = (
                        f"/vd delete {shlex.quote(str(world['id']))} confirm "
                        f"{shlex.quote(str(world['name']))}"
                    )
                    yield event.plain_result(
                        f"即将永久删除世界「{world['name']}」（{world['id']}）。"
                        "世界数据将被删除，已有服务端备份不受影响。"
                        f"\n确认请原样发送：{confirmation}"
                    )
                    return
                if (
                    len(tokens) < 4
                    or tokens[2].lower() not in {"confirm", "确认"}
                    or not " ".join(tokens[3:]).strip()
                ):
                    yield event.plain_result("用法：/vd delete <世界ID>；确认时使用 /vd delete <世界ID> confirm <完整世界名称>")
                    return
                confirmed_name = " ".join(tokens[3:])
                result = await asyncio.to_thread(
                    self.client.admin_delete_world,
                    tokens[1],
                    confirmed_name,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                )
                disconnected = result.get("online_players_disconnected") or []
                suffix = (
                    f" 在线玩家已断开：{'、'.join(str(name) for name in disconnected)}。"
                    if disconnected
                    else ""
                )
                yield event.plain_result(
                    f"世界「{result.get('name', confirmed_name)}」已永久删除。"
                    f"{'当前世界选择已清除。' if result.get('was_current') else ''}{suffix}"
                )
                return
            if subcommand in {"pause", "暂停", "resume", "恢复"}:
                if len(tokens) > 2:
                    yield event.plain_result("用法：/vd pause|resume [世界ID|current]")
                    return
                sender_id = self._qq_sender(event)
                if not self.admin_qq:
                    yield event.plain_result("AstrBot 插件尚未配置管理员 QQ 号，QQ 管理功能未启用。")
                    return
                paused = subcommand in {"pause", "暂停"}
                worlds = await asyncio.to_thread(self.client.worlds)
                target = tokens[1] if len(tokens) == 2 else "current"
                if target.lower() == "current":
                    current = next((item for item in worlds.get("worlds", []) if item.get("current")), None)
                    if current is None:
                        yield event.plain_result("当前没有运行中的世界。")
                        return
                    world_id = str(current["id"])
                else:
                    world_id = target
                result = await asyncio.to_thread(
                    self.client.admin_set_world_paused,
                    world_id,
                    sender_qq_id=sender_id,
                    configured_admin_qq=self.admin_qq,
                    paused=paused,
                )
                label = "已暂停" if result.get("status") == "paused" else "已恢复"
                yield event.plain_result(f"{label}世界 {world_id}。")
                return
            if subcommand in {"action", "行动", "chat", "聊天"}:
                yield event.plain_result("行动和游戏内聊天请使用虚拟域桌面客户端。")
                return
            yield event.plain_result("未知子命令，使用 /vd help 查看帮助")
        except ConnectorError as exc:
            yield event.plain_result(f"虚拟域：{exc}")

    async def terminate(self):
        if self._bridge_task:
            self._bridge_task.cancel()
            await asyncio.gather(self._bridge_task, return_exceptions=True)
        pending = list(self._bridge_requests)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        logger.info("[虚拟域] 连接器已卸载")

