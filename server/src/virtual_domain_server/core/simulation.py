"""世界模型结构化推演入口。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from collections.abc import Callable

from .providers import ModelProvider, ModelProviderError, ModelResponse
from .state_schema import validate_state_patch, validate_state_schema


DANGEROUS_SITUATION_CUES = (
    "陷入险境", "身处险境", "处于危险", "遭遇危险", "生命危险", "面临危险",
    "被绑架", "遭绑架", "被挟持", "遭到袭击", "受到攻击", "被攻击", "被追杀",
    "被偷袭", "遭伏击", "被困住", "被困在", "中毒", "溺水", "落水", "失踪",
    "遭遇战斗", "被卷入战斗", "被刺伤", "被枪击", "受重伤", "抓住", "抓起来",
    "拽住", "拖走", "推倒", "打倒", "打伤", "踢倒", "勒住", "掐住", "按住",
    "绑起来", "关起来", "锁起来", "强迫", "威胁", "抢走", "伤害", "攻击",
    "刺伤", "砍伤", "打晕", "下毒", "抢劫", "偷袭", "性侵", "侵犯", "折磨",
    "残废", "in danger", "in peril", "was kidnapped", "is kidnapped",
    "was attacked", "is attacked", "was captured", "is captured", "went missing",
    "is missing", "was poisoned", "is poisoned", "was trapped", "is trapped",
    "grabbed", "grab", "dragged", "drag", "pushed", "push", "shoved", "shove",
    "restrained", "restrain", "tied up", "locked up", "forced", "threatened",
    "threaten", "assault", "attack", "kill", "injure", "kidnap", "poison",
    "rob", "torture", "stab", "shoot", "choked", "choke",
)
DANGER_NEGATIONS = (
    "没有", "并未", "未曾", "不曾", "并没有", "并非", "未能", "不会", "没能",
    "未", "没", "not ", "never ", "n't ",
)
MIN_HIDDEN_WORLDVIEW_MATCH_CHARS = 24


def contains_unnegated_cue(text: str, cues: tuple[str, ...]) -> bool:
    lowered = text.casefold()
    for cue in cues:
        start = 0
        normalized_cue = cue.casefold()
        while (position := lowered.find(normalized_cue, start)) >= 0:
            if not any(
                lowered[max(0, position - len(negation)):position].endswith(negation)
                for negation in DANGER_NEGATIONS
            ):
                return True
            start = position + len(normalized_cue)
    return False


def contains_dangerous_situation(text: str) -> bool:
    return contains_unnegated_cue(text, DANGEROUS_SITUATION_CUES)


class SimulationError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        actual_tokens: int = 0,
        code: str = "simulation_failed",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        # 重试中已经产生实际用量的调用必须由上层入账，即使最终没有提交节点。
        self.actual_tokens = actual_tokens
        self.code = code
        self.retryable = retryable


@dataclass(slots=True)
class SimulationInput:
    world_id: str
    state_version: int
    world_state: dict[str, Any]
    action: str
    visible_context: str = ""
    shallow_worldview: str = ""
    deep_worldview: str = ""
    time_context: dict[str, Any] = field(default_factory=dict)
    consequence_rules: dict[str, bool] = field(default_factory=dict)
    participants: list[dict[str, Any]] = field(default_factory=list)
    interaction_responses: list[dict[str, Any]] = field(default_factory=list)
    actor_id: str | None = None
    actor_name: str | None = None
    state_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SimulationResult:
    narrative: str
    state_patch: dict[str, Any] = field(default_factory=dict)
    memory_entries: list[dict[str, Any]] = field(default_factory=list)
    time_advance: str | None = None
    pending_interactions: list[dict[str, Any]] = field(default_factory=list)
    response: ModelResponse | None = None
    token_usage: int = 0
    player_effects: list[dict[str, Any]] | None = None
    retry_required: bool = False
    retry_reason: str = ""
    visible_to: list[str] | None = None
    world_end: bool = False
    ending_reason: str = ""
    world_end_declared: bool = False

    @property
    def actual_tokens(self) -> int | None:
        return self.token_usage if self.response and self.response.usage_supported else None


class WorldSimulationEngine:
    """把模型输出限制在可校验的世界推演结构内。"""

    def __init__(self, provider: ModelProvider, *, max_retries: int = 2) -> None:
        self.provider = provider
        self.max_retries = max(0, max_retries)

    def run(
        self,
        request: SimulationInput,
        *,
        max_tokens: int | None = None,
        before_call: Callable[[], None] | None = None,
        on_usage: Callable[[int], None] | None = None,
        reserve_call: Callable[[list[dict[str, str]], int, int], str] | None = None,
        settle_call: Callable[[str, int], None] | None = None,
        release_call: Callable[[str], None] | None = None,
        validate_result: Callable[[SimulationInput, SimulationResult], None] | None = None,
    ) -> SimulationResult:
        last_error: Exception | None = None
        consumed = 0
        provider = self.provider
        if max_tokens is None:
            max_tokens = getattr(provider.config, "max_output_tokens", None)
            if max_tokens is None:
                max_tokens = 8192
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens < 1:
            raise ValueError("世界推演输出 Token 上限必须是正整数")
        for attempt in range(self.max_retries + 1):
            messages = self._messages(request)
            if attempt and isinstance(
                last_error,
                (SimulationError, json.JSONDecodeError, TypeError, ValueError),
            ):
                error_code = getattr(last_error, "code", "invalid_response")
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "上一轮输出未通过服务端校验"
                            f"（{error_code}）。请从头修正推演，严格按系统要求只返回一个 JSON 对象；"
                            "不要输出引号包裹的 JSON、Markdown 代码围栏或额外说明。"
                        ),
                    }
                )
            reservation_id: str | None = None
            settled = False
            try:
                if before_call:
                    before_call()
                if reserve_call:
                    reservation_id = reserve_call(messages, max_tokens, attempt)
                complete_structured = getattr(provider, "complete_structured", None)
                if callable(complete_structured):
                    response = complete_structured(
                        messages,
                        response_schema=self._response_schema(request.state_schema),
                        max_tokens=max_tokens,
                        temperature=0.7,
                    )
                else:
                    response = provider.complete(messages, max_tokens=max_tokens, temperature=0.7)
                if response.total_tokens is not None:
                    if response.total_tokens > 0:
                        consumed += response.total_tokens
                    if response.total_tokens > 0 and reservation_id and settle_call:
                        settle_call(reservation_id, response.total_tokens)
                        settled = True
                    elif response.total_tokens > 0 and on_usage:
                        on_usage(response.total_tokens)
                if not response.usage_supported:
                    raise SimulationError("世界模型未返回实际 Token 用量，不能结算本次推演", actual_tokens=consumed)
                if response.total_tokens is None or response.total_tokens <= 0:
                    raise SimulationError("世界模型未产生可计费 Token 用量", actual_tokens=consumed, code="usage_unavailable")
                result = self._parse(response)
                if validate_result:
                    validate_result(request, result)
                self.validate_consequences(request, result)
                self.validate_visibility(request, result)
                self.validate_narrative_privacy(request, result)
                try:
                    validate_state_patch(request.state_schema, result.state_patch)
                except ValueError as exc:
                    raise SimulationError(str(exc), code="invalid_state_patch") from exc
                result.response = response
                result.token_usage = consumed
                return result
            except (ModelProviderError, SimulationError, json.JSONDecodeError, TypeError, ValueError) as exc:
                last_error = exc
                if isinstance(exc, ModelProviderError) and not exc.retryable:
                    break
                if isinstance(exc, SimulationError) and not exc.retryable:
                    break
            finally:
                if reservation_id and not settled and release_call:
                    release_call(reservation_id)
        raise SimulationError(f"世界推演失败：{last_error}", actual_tokens=consumed, code=getattr(last_error, "code", "simulation_failed")) from last_error

    def _messages(self, request: SimulationInput) -> list[dict[str, str]]:
        schema = {
            "narrative": "世界模拟器的完整叙事，服务端按 visible_to 向玩家筛选",
            "visible_to": "本次行动叙事的观察者账号 ID 数组；可用 bot 或 *（仅确实所有人都能观察时）。叙事中直接提到玩家或 Bot 名字时，该参与者必须在可见范围内。缺省时服务端只向行动者显示",
            "state_patch": "对当前完整世界状态的最小 JSON 对象修改；这是模拟器内部状态，不直接发送给玩家",
            "state_schema": "管理员定义的规则关键状态字段类型和范围；配置后只能修改已声明字段，且必须符合规则",
            "memory_entries": "数组，每项为可检索经历对象",
            "time_advance": "世界时间变化的描述；没有时间变化时返回 null",
            "pending_interactions": "数组，需要其他玩家回应的交互；每项含 target_id、prompt、risk_level(low/normal/high)、dangerous(bool)",
            "retry_required": "布尔值；等待期间世界状态改变导致行动失效时为 true",
            "retry_reason": "retry_required 为 true 时给行动者的简短原因",
            "player_effects": "数组，每项包含 target_id（玩家账号 ID 或 bot）、dangerous_situation（布尔，是否陷入危险或受到威胁，即使没有受伤）、death（布尔）、permanent_negative（布尔）、long_term（布尔）；若有离线玩家或正在补算玩家离开期间的时间，必须逐一列出每位受保护玩家并将危险和重大后果标记为 false；没有角色影响时返回空数组",
            "world_end": "布尔值；世界唯一参演者发生死亡后必须明确填写；只有世界规则明确绝无复活可能时为 true，否则为 false",
            "ending_reason": "world_end 为 true 时说明该角色为何无法复活以及世界为何结束；其他情况为空字符串",
        }
        system = (
            "你是虚拟域的世界模拟器和可信裁定者，可以读取完整世界状态与深层设定；玩家与 Bot 本身不能读取这些数据。"
            "只根据当前状态、行动和观察关系进行推演。interaction_responses 中实际回应的玩家与本次行动者共同参与了当前交互结算，可将他们加入本次事件可见范围。严格只返回 JSON，不要 Markdown。"
            "输入中的 actor.account_id 与 actor.display_name 明确标识本次行动者；participants 中同一 account_id 的玩家就是这次行动者，不得把该玩家误认为场外观察者。"
            "每次必须按实际场景填写 visible_to：只列亲眼看到、听到或通过已建立渠道获知事件的参与者；不在现场的人不能因为属于同一世界而自动知道。* 仅用于所有玩家都可观察的公开事件。"
            "visible_to 控制整条世界屏幕记录及其经历检索的可见范围；玩家始终可以看到自己的行动结果。"
            "state_patch 不得包含 event_count、last_event、memory_entries 等服务端维护字段。"
            "若行动会直接影响其他玩家并需要其回应，先不要提交世界状态修改；在 pending_interactions 中列出目标账号 ID、给玩家看的说明、风险等级和 dangerous。"
            "若收到 interaction_responses，结合回应和最新世界状态完成裁定；若行动因等待期间发生的变化而失效，设置 retry_required=true，清空 state_patch。"
            "目标玩家超时未回应时，由模拟器依据该玩家人设和关系代为作出反应；对离线玩家只允许普通、安全互动，拒绝危险操作。"
            "存在离线玩家或正在补算玩家离开期间的时间时，player_effects 必须逐一包含每位受保护玩家；dangerous_situation 为 true 表示角色进入险境、遭受威胁、攻击、绑架、胁迫或其他可能受伤的危险，即使最后没有实际受伤。受保护玩家的 dangerous_situation、death、permanent_negative、long_term 必须全部为 false；普通且安全的互动仍可正常发生。"
            "player_effects 必须与本次 narrative 中对具名玩家的重大后果一致；不能只在结构字段或叙事中单方面声明。"
            "当整个世界恰好只有一位参演玩家（participants 包含全部人类玩家和 Bot）且该玩家在 player_effects 中明确死亡时，必须显式设置 world_end。只有世界规则明确不存在任何复活方式时才设置 world_end=true 并说明 ending_reason；若存在复活方式，设置 false。"
            "若世界规则存在复活方式、死亡只是暂时状态、参演者不止一位，world_end 必须为 false。玩家不能切换角色或增加角色；多人世界中一名玩家死亡不得结束整个世界。"
            f"输出字段要求：{json.dumps(schema, ensure_ascii=False)}\n"
            f"浅层世界设定：{request.shallow_worldview}\n"
            f"深层世界设定（仅模拟器可见）：{request.deep_worldview}"
        )
        if request.time_context:
            system += (
                "\n这是流动式世界，只结算 time_context 给出的尚未结算间隔，不能重复推进。"
                "时间补算一次概括整个间隔，不按小时逐次推演。"
                "只有 may_routine 为 true 的玩家才能补写符合人设、对世界影响很小的日常行为。"
                "日常行为不能涉及关系重大变化、重要资源消耗或重大决定。"
                "所有离开期间的补写都必须避免死亡、永久负面效果、长期后果和危险境地。"
                "Bot 行动时不得控制用户；若试图对离线用户实施危险行为，必须以该用户离线为由拒绝。"
                "玩家回归时说明角色离开期间的经历，但不能杜撰已经结算的时间段。"
            )
        if request.time_context.get("trigger") == "admin_once":
            system += (
                "\n本次推演由管理员明确授权，用于让一份待生效配置现在生效。"
                "这不是任何玩家的行动，也不得替玩家决定行动、关系、立场或重大选择。"
                "只允许按新配置推演世界环境和 NPC 的日常变化；不得提议或叙述玩家状态变更，"
                "不得创建待回应交互。若没有合适的自然变化，可以只推进很小幅度的环境变化。"
            )
        system += f"\n角色重大后果开关：{json.dumps(request.consequence_rules, ensure_ascii=False)}；未开启的重大后果不能产生，离线保护始终优先。"
        user = json.dumps({"world_id": request.world_id, "state_version": request.state_version, "actor": {"account_id": request.actor_id, "display_name": request.actor_name}, "world_state": request.world_state, "state_schema": request.state_schema, "visible_context": request.visible_context, "participants": request.participants, "action": request.action, "time_context": request.time_context, "interaction_responses": request.interaction_responses}, ensure_ascii=False)
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    @staticmethod
    def _response_schema(state_schema: dict[str, Any]) -> dict[str, Any]:
        declared_state = validate_state_schema(state_schema)

        def convert_rule(rule: dict[str, Any]) -> dict[str, Any]:
            property_schema: dict[str, Any] = {"type": rule["type"]}
            if rule["type"] == "object":
                property_schema["additionalProperties"] = rule.get(
                    "additional_properties",
                    "properties" not in rule,
                )
                if "properties" in rule:
                    property_schema["properties"] = {
                        name: convert_rule(child)
                        for name, child in rule["properties"].items()
                    }
                if "required" in rule:
                    property_schema["required"] = rule["required"]
            elif rule["type"] == "array":
                property_schema["items"] = (
                    convert_rule(rule["items"]) if "items" in rule else {}
                )
                for bound, keyword in (
                    ("min_length", "minItems"),
                    ("max_length", "maxItems"),
                ):
                    if bound in rule:
                        property_schema[keyword] = rule[bound]
            if rule["type"] in {"number", "integer"}:
                for bound in ("minimum", "maximum"):
                    if bound in rule:
                        property_schema[bound] = rule[bound]
            choices = rule.get("enum")
            if isinstance(choices, list):
                if rule["type"] == "string" and all(isinstance(item, str) for item in choices):
                    property_schema["enum"] = choices
                elif rule["type"] in {"number", "integer"} and all(
                    isinstance(item, (int, float)) and not isinstance(item, bool)
                    for item in choices
                ):
                    property_schema["enum"] = choices
            return property_schema

        state_properties = {
            name: convert_rule(rule)
            for name, rule in declared_state.items()
        }

        state_patch: dict[str, Any] = {
            "type": "object",
            "additionalProperties": not bool(state_properties),
        }
        if state_properties:
            state_patch["properties"] = state_properties
        return {
            "type": "object",
            "properties": {
                "narrative": {"type": "string"},
                "visible_to": {"type": "array", "items": {"type": "string"}},
                "state_patch": state_patch,
                "memory_entries": {
                    "type": "array",
                    "items": {"type": "object", "additionalProperties": True},
                },
                "time_advance": {"type": ["string", "null"]},
                "pending_interactions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_id": {"type": "string"},
                            "prompt": {"type": "string"},
                            "risk_level": {"type": "string", "enum": ["low", "normal", "high"]},
                            "dangerous": {"type": "boolean"},
                        },
                        "required": ["target_id", "prompt", "risk_level", "dangerous"],
                    },
                },
                "retry_required": {"type": "boolean"},
                "retry_reason": {"type": "string"},
                "player_effects": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "target_id": {"type": "string"},
                            "dangerous_situation": {"type": "boolean"},
                            "death": {"type": "boolean"},
                            "permanent_negative": {"type": "boolean"},
                            "long_term": {"type": "boolean"},
                        },
                        "required": ["target_id", "dangerous_situation", "death", "permanent_negative", "long_term"],
                    },
                },
                "world_end": {"type": "boolean"},
                "ending_reason": {"type": "string"},
            },
            "required": [
                "narrative",
                "visible_to",
                "state_patch",
                "memory_entries",
                "time_advance",
                "pending_interactions",
                "retry_required",
                "retry_reason",
                "player_effects",
                "world_end",
                "ending_reason",
            ],
            "additionalProperties": False,
        }

    def _parse(self, response: ModelResponse) -> SimulationResult:
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3].rstrip()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as first_error:
            value = None
            decoder = json.JSONDecoder()
            offset = raw.find("{")
            while offset >= 0:
                try:
                    candidate, _end = decoder.raw_decode(raw, offset)
                except json.JSONDecodeError:
                    offset = raw.find("{", offset + 1)
                    continue
                if isinstance(candidate, dict):
                    value = candidate
                    break
                offset = raw.find("{", offset + 1)
            if value is None:
                if not raw:
                    detail = "响应文本为空"
                else:
                    detail = f"响应不是 JSON（长度 {len(raw)}，首字符 U+{ord(raw[0]):04X}）"
                diagnostic = self._response_diagnostic(response)
                raise SimulationError(
                    f"世界模型未返回有效 JSON：{detail}{diagnostic}",
                    code="invalid_json",
                ) from first_error
        if not isinstance(value, dict):
            raise SimulationError("世界模型输出必须是 JSON 对象")
        narrative = value.get("narrative")
        patch = value.get("state_patch", {})
        memories = value.get("memory_entries", [])
        pending = value.get("pending_interactions", [])
        if not isinstance(narrative, str) or not narrative.strip():
            raise SimulationError("世界模型缺少 narrative")
        if not isinstance(patch, dict) or not isinstance(memories, list) or not isinstance(pending, list):
            raise SimulationError("世界模型输出字段类型错误")
        result = SimulationResult(narrative.strip(), patch, memories, value.get("time_advance"), pending)
        retry_required = value.get("retry_required", False)
        if not isinstance(retry_required, bool):
            raise SimulationError("retry_required 必须是布尔值")
        result.retry_required = retry_required
        result.retry_reason = str(value.get("retry_reason", "")).strip()
        visible_to = value.get("visible_to")
        if visible_to is not None and (not isinstance(visible_to, list) or any(not isinstance(item, str) for item in visible_to)):
            raise SimulationError("visible_to 必须是观察者 ID 字符串数组")
        result.visible_to = visible_to
        effects = value.get("player_effects")
        if effects is not None and not isinstance(effects, list):
            raise SimulationError("player_effects 必须是数组")
        result.player_effects = effects
        world_end = value.get("world_end", False)
        ending_reason = value.get("ending_reason", "")
        if not isinstance(world_end, bool):
            raise SimulationError("world_end 必须是布尔值")
        if not isinstance(ending_reason, str):
            raise SimulationError("ending_reason 必须是字符串")
        result.world_end = world_end
        result.ending_reason = ending_reason.strip()
        result.world_end_declared = "world_end" in value
        return result

    @staticmethod
    def _response_diagnostic(response: ModelResponse) -> str:
        raw = response.raw
        if not isinstance(raw, dict):
            return ""
        candidates = raw.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates else {}
        details: list[str] = []
        finish_reason = candidate.get("finishReason") if isinstance(candidate, dict) else None
        if isinstance(finish_reason, str):
            safe_reason = re.sub(r"[^A-Za-z0-9_]", "", finish_reason)[:40]
            if safe_reason:
                details.append(f"finishReason={safe_reason}")
        usage = raw.get("usageMetadata")
        if isinstance(usage, dict):
            for key in (
                "promptTokenCount",
                "candidatesTokenCount",
                "thoughtsTokenCount",
                "totalTokenCount",
            ):
                count = usage.get(key)
                if type(count) is int and count >= 0:
                    details.append(f"{key}={count}")
        if not details:
            return ""
        return "（" + ", ".join(details) + "）"

    def validate_visibility(self, request: SimulationInput, result: SimulationResult) -> None:
        audience = list(result.visible_to or [])
        known = {str(player["account_id"]) for player in request.participants} | {"bot", "*"}
        if request.actor_id:
            known.add(request.actor_id)
            # The player who submitted an action always sees its result.
            if "*" not in audience and request.actor_id not in audience:
                audience.append(request.actor_id)
        if "*" not in audience:
            # A committed settlement after a response is shared with the actor
            # and each responding participant, who took part in the same scene.
            for response in request.interaction_responses:
                target_id = str(response.get("target_id", ""))
                if target_id in known and target_id not in audience:
                    audience.append(target_id)
        if len(audience) != len(set(audience)):
            raise SimulationError("visible_to 不能重复列出参与者")
        if any(not item or item not in known for item in audience):
            raise SimulationError("visible_to 引用了未知参与者")
        if "*" in audience and len(audience) != 1:
            raise SimulationError("visible_to 使用 * 时不能再列出其他参与者")
        # A proposed interaction has not been committed to the world screen.
        # Its target-specific prompt is validated and delivered separately.
        if result.pending_interactions:
            return
        # If the narrative names a participant, do not send that text to a
        # different player's view. The action actor is added above because the
        # service always guarantees the actor can see their own result.
        if "*" not in audience and (result.visible_to is not None or request.actor_id is not None):
            for participant in request.participants:
                account_id = str(participant.get("account_id", ""))
                aliases = {
                    str(participant.get(field, "")).strip()
                    for field in ("display_name", "character_name")
                    if str(participant.get(field, "")).strip()
                }
                if account_id == "bot":
                    aliases.add("Bot")
                if account_id and account_id not in audience and any(
                    alias.casefold() in result.narrative.casefold() for alias in aliases
                ):
                    raise SimulationError(
                        "叙事提到了不在可见范围内的参与者",
                        code="visibility_mismatch",
                    )

    def validate_narrative_privacy(self, request: SimulationInput, result: SimulationResult) -> None:
        """Reject long verbatim deep-worldview passages, not semantic paraphrases."""
        narrative = self._normalize_fact_text(result.narrative)
        shallow = self._normalize_fact_text(request.shallow_worldview)
        if not narrative:
            return
        for sentence in re.split(r"[。！？!?；;：:\n]+", request.deep_worldview):
            hidden_fact = self._normalize_fact_text(sentence.lstrip("-*•# "))
            if len(hidden_fact) < MIN_HIDDEN_WORLDVIEW_MATCH_CHARS or hidden_fact in shallow:
                continue
            if hidden_fact in narrative:
                raise SimulationError(
                    "世界叙事逐字泄露了深层设定",
                    code="hidden_worldview_leak",
                )

    @staticmethod
    def _normalize_fact_text(value: str) -> str:
        return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)

    def validate_consequences(self, request: SimulationInput, result: SimulationResult) -> None:
        timeline_players = request.time_context.get("players", [])
        participants = request.participants or timeline_players
        protected = {
            str(player["account_id"])
            for player in participants
            if not player.get("online") or request.time_context.get("trigger") == "return"
        }
        if protected and result.player_effects is None:
            raise SimulationError("离线玩家存在时必须返回 player_effects，不能确认离线保护", code="offline_protection")
        known = {str(player["account_id"]) for player in participants} | {str(player["account_id"]) for player in timeline_players} | {"bot"}
        effect_targets: set[str] = set()
        for effect in result.player_effects or []:
            if not isinstance(effect, dict) or not isinstance(effect.get("target_id"), str):
                raise SimulationError("player_effects 目标格式错误")
            target_id = effect["target_id"]
            if target_id not in known:
                raise SimulationError("player_effects 引用了未知玩家")
            if target_id in effect_targets:
                raise SimulationError("player_effects 不能重复列出玩家")
            effect_targets.add(target_id)
            if "dangerous_situation" in effect and not isinstance(effect["dangerous_situation"], bool):
                raise SimulationError("dangerous_situation 必须是布尔值")
            if target_id in protected and "dangerous_situation" not in effect:
                raise SimulationError(
                    "离线玩家必须明确声明 dangerous_situation",
                    code="offline_protection",
                )
            for key in ("death", "permanent_negative", "long_term"):
                if not isinstance(effect.get(key), bool):
                    raise SimulationError("player_effects 必须明确声明危险和所有重大后果")
            if effect.get("dangerous_situation") is True and target_id in protected:
                raise SimulationError("世界提案将离线玩家置于危险情境", code="offline_protection")
            for key in ("death", "permanent_negative", "long_term"):
                if effect[key] and (target_id in protected or not request.consequence_rules.get(key, False)):
                    raise SimulationError(
                        f"世界提案违反离线保护或重大后果开关：{key}",
                        code="offline_protection" if target_id in protected else "consequence_disabled",
                    )
        if protected - effect_targets:
            raise SimulationError(
                "player_effects 必须逐一声明每位离线玩家的危险状态",
                code="offline_protection",
            )

    def validate_narrative_consequences(self, request: SimulationInput, result: SimulationResult) -> None:
        timeline_players = request.time_context.get("players", [])
        participants = request.participants or timeline_players
        cues = {
            "dangerous_situation": DANGEROUS_SITUATION_CUES,
            "death": (
                "死亡", "死了", "死去", "身亡", "丧命", "被杀", "杀死", "杀害", "害死",
                "停止呼吸", "再也没有醒来", "died", "dead", "killed",
            ),
            "permanent_negative": (
                "永久失明", "永久失聪", "永久残疾", "永久瘫痪", "永久毁容", "永久截肢",
                "不可逆损伤", "终身残疾", "permanent disability", "permanent injury",
                "irreversible injury", "permanently blind", "permanently paralyzed",
            ),
            "long_term": (
                "长期无法行动", "长期失去行动能力", "终身无法行动", "余生无法行动",
                "再也无法行走", "再也不能行动", "lifelong inability", "long-term incapacity",
            ),
        }
        participant_aliases: dict[str, set[str]] = {}
        for participant in participants:
            target_id = str(participant.get("account_id", ""))
            aliases = {
                str(participant.get(field, "")).strip()
                for field in ("display_name", "character_name")
                if str(participant.get(field, "")).strip()
            }
            if target_id and aliases:
                participant_aliases[target_id] = aliases

        claims: dict[str, set[str]] = {}
        sentences = re.split(r"[。！？!?；;\n]+", result.narrative)
        offline_players = {
            str(player.get("account_id", ""))
            for player in participants
            if not player.get("online") or request.time_context.get("trigger") == "return"
        }
        for target_id, aliases in participant_aliases.items():
            for sentence in sentences:
                lowered = sentence.casefold()
                if not any(alias.casefold() in lowered for alias in aliases):
                    continue
                if target_id in offline_players:
                    if contains_dangerous_situation(lowered):
                        raise SimulationError(
                            "叙事将离线玩家置于危险情境，已拒绝提交",
                            code="offline_protection",
                        )
                for consequence, terms in cues.items():
                    if consequence == "dangerous_situation" and target_id not in offline_players:
                        continue
                    if contains_unnegated_cue(lowered, terms):
                        claims.setdefault(target_id, set()).add(consequence)

        effects_by_id = {
            str(effect["target_id"]): effect
            for effect in result.player_effects or []
            if isinstance(effect, dict) and isinstance(effect.get("target_id"), str)
        }
        for target_id, declared_effect in effects_by_id.items():
            for consequence in cues:
                if declared_effect.get(consequence) and consequence not in claims.get(target_id, set()):
                    raise SimulationError(
                        f"结构化玩家后果与叙事不一致：{consequence}",
                        code="consequence_mismatch",
                    )
        for target_id, narrative_claims in claims.items():
            declared_effect = effects_by_id.get(target_id, {})
            for consequence in narrative_claims:
                if declared_effect.get(consequence) is not True:
                    raise SimulationError(
                        f"叙事与结构化玩家后果不一致：{consequence}",
                        code="consequence_mismatch",
                    )
