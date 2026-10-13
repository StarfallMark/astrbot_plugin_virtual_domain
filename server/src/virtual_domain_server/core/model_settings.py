"""Runtime model profiles, encrypted persistence, and a shared call limit."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import asdict
from typing import Any, Iterator

from .memory import WorldMemory
from .providers import ModelConfig, ModelProvider, provider_from_config
from .secret_store import EncryptedConfigStore
from .service import Conflict
from .simulation import WorldSimulationEngine


class ModelCallLimiter:
    """A runtime-adjustable limit shared by generation and memory calls."""

    def __init__(self, maximum: int = 2) -> None:
        self._condition = threading.Condition()
        self._maximum = self._validate(maximum)
        self._active = 0

    @staticmethod
    def _validate(maximum: int) -> int:
        if isinstance(maximum, bool) or not isinstance(maximum, int) or not 1 <= maximum <= 32:
            raise ValueError("模型并发上限必须是 1 到 32 的整数")
        return maximum

    @property
    def maximum(self) -> int:
        with self._condition:
            return self._maximum

    def set_maximum(self, maximum: int) -> None:
        validated = self._validate(maximum)
        with self._condition:
            self._maximum = validated
            self._condition.notify_all()

    @contextmanager
    def slot(self) -> Iterator[None]:
        with self._condition:
            self._condition.wait_for(lambda: self._active < self._maximum)
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()


class LimitedModelProvider:
    def __init__(
        self,
        provider: ModelProvider,
        limiter: ModelCallLimiter,
        *,
        role: str = "world",
        usage_support_recorder=None,
    ) -> None:
        self._provider = provider
        self._limiter = limiter
        self._role = role
        self._usage_support_recorder = usage_support_recorder

    @property
    def config(self) -> ModelConfig:
        return self._provider.config

    def complete(self, messages, *, max_tokens: int = 1024, temperature: float = 0.7):
        with self._limiter.slot():
            response = self._provider.complete(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        self._record_usage_support(response)
        return response

    def complete_structured(
        self,
        messages,
        *,
        response_schema: dict[str, Any],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ):
        complete_structured = getattr(self._provider, "complete_structured", None)
        if not callable(complete_structured):
            return self.complete(messages, max_tokens=max_tokens, temperature=temperature)
        with self._limiter.slot():
            response = complete_structured(
                messages,
                response_schema=response_schema,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        self._record_usage_support(response)
        return response

    def _record_usage_support(self, response: Any) -> None:
        supported = getattr(response, "usage_supported", None)
        if not isinstance(supported, bool) or not callable(self._usage_support_recorder):
            return
        try:
            self._usage_support_recorder(self._role, supported)
        except Exception:
            import logging

            logging.getLogger("virtual_domain").exception(
                "Could not persist model usage capability for role %s",
                self._role,
            )


class ModelSettingsManager:
    """Manage role-based model profiles without returning stored credentials."""

    GENERATIVE_ROLES = {"world", "bot"}
    MEMORY_ROLES = {"embedding", "rerank"}
    WORLD_OUTPUT_DEFAULT = 8192
    WORLD_OUTPUT_DEFAULT_VERSION = 2
    PREVIOUS_WORLD_OUTPUT_DEFAULT = 2048

    def __init__(
        self,
        service: Any,
        store: EncryptedConfigStore,
        *,
        bot_mode: str,
    ) -> None:
        self.service = service
        self.store = store
        self.bot_mode = bot_mode
        self.limiter = ModelCallLimiter()
        self._lock = threading.RLock()
        loaded = store.load()
        if loaded is None:
            self._profiles = self._initial_profiles()
            self._profiles["bot_mode"] = bot_mode
            store.save(self._profiles)
        else:
            self._profiles = loaded
            saved_mode = self._profiles.get("bot_mode", bot_mode)
            if saved_mode not in {"standalone", "astrbot"}:
                raise ValueError("已保存的 Bot 运行模式无效")
            self.bot_mode = saved_mode
            self._profiles["bot_mode"] = saved_mode
            changed = self._ensure_usage_support()
            changed = self._migrate_world_output_default() or changed
            if changed:
                store.save(self._profiles)
        service.bot_mode = self.bot_mode
        self._validate_all(self._profiles)
        self.service._usage_support_recorder = self.record_usage_support
        self.service._usage_support_status_reader = self.usage_support_status
        self._apply()

    def _initial_profiles(self) -> dict[str, Any]:
        world = None
        if self.service.simulation_engine:
            world = asdict(self.service.simulation_engine.provider.config)
            world["max_output_tokens"] = (
                world.get("max_output_tokens") or self.WORLD_OUTPUT_DEFAULT
            )
            world["worldview_max_output_tokens"] = (
                world.get("worldview_max_output_tokens") or 3072
            )
        bot = None
        if self.bot_mode != "astrbot" and self.service.chat_provider:
            config = self.service.chat_provider.config
            bot = {
                "provider": config.provider,
                "model": config.model,
                "api_key": config.api_key,
                "endpoint": config.endpoint,
                "timeout": config.timeout,
                "max_output_tokens": getattr(config, "max_output_tokens", None) or 1024,
            }
        memory = self.service.memory
        return {
            "version": 1,
            "revision": 1,
            "world_output_default_version": self.WORLD_OUTPUT_DEFAULT_VERSION,
            "world": world,
            "bot": bot,
            "embedding": dict(memory.embedding) if memory else None,
            "rerank": dict(memory.rerank) if memory else None,
            "max_concurrent_calls": 2,
            "usage_support": {
                role: "unknown" for role in ("world", "bot", "embedding", "rerank")
            },
        }

    def _ensure_usage_support(self) -> bool:
        current = self._profiles.get("usage_support")
        if not isinstance(current, dict):
            current = {}
        normalized = {
            role: current.get(role)
            if isinstance(current.get(role), str)
            and current.get(role) in {"unknown", "supported", "missing"}
            else "unknown"
            for role in ("world", "bot", "embedding", "rerank")
        }
        changed = normalized != current
        self._profiles["usage_support"] = normalized
        return changed

    def _migrate_world_output_default(self) -> bool:
        version = self._profiles.get("world_output_default_version", 1)
        if isinstance(version, bool) or not isinstance(version, int):
            version = 1
        if version >= self.WORLD_OUTPUT_DEFAULT_VERSION:
            return False

        revision = self._profiles.get("revision", 1)
        world = self._profiles.get("world")
        if (
            type(revision) is int
            and revision == 1
            and isinstance(world, dict)
            and world.get("max_output_tokens") == self.PREVIOUS_WORLD_OUTPUT_DEFAULT
        ):
            world["max_output_tokens"] = self.WORLD_OUTPUT_DEFAULT
        self._profiles["world_output_default_version"] = self.WORLD_OUTPUT_DEFAULT_VERSION
        return True

    def usage_support_status(self) -> dict[str, str]:
        with self._lock:
            self._ensure_usage_support()
            return dict(self._profiles["usage_support"])

    def record_usage_support(self, role: str, supported: bool) -> None:
        if role not in {"world", "bot", "embedding", "rerank"}:
            return
        status = "supported" if supported else "missing"
        with self._lock:
            self._ensure_usage_support()
            current = self._profiles["usage_support"].get(role)
            if current == status or (current == "missing" and supported):
                return
            profiles = dict(self._profiles)
            profiles["usage_support"] = dict(self._profiles["usage_support"])
            profiles["usage_support"][role] = status
            self.store.save(profiles)
            self._profiles = profiles

    @staticmethod
    def _model_config(value: Any, role: str) -> ModelConfig:
        if not isinstance(value, dict):
            raise ValueError(f"{role} 模型配置必须是对象")
        provider = str(value.get("provider", "")).strip()
        model = str(value.get("model", "")).strip()
        api_key = str(value.get("api_key", ""))
        endpoint = str(value.get("endpoint", "")).strip()
        timeout = value.get("timeout", 120)
        is_world = role in {"world", "世界"}
        default_output_tokens = ModelSettingsManager.WORLD_OUTPUT_DEFAULT if is_world else 1024
        max_output_tokens = value.get("max_output_tokens")
        if max_output_tokens is None:
            max_output_tokens = default_output_tokens
        worldview_max_output_tokens = value.get("worldview_max_output_tokens")
        if worldview_max_output_tokens is None:
            worldview_max_output_tokens = 3072
        if not provider or not model:
            raise ValueError(f"{role} 模型供应商和模型名称不能为空")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= float(timeout) <= 600:
            raise ValueError(f"{role} 模型超时必须是 1 到 600 秒")
        for field_name, limit in (
            ("max_output_tokens", max_output_tokens),
            ("worldview_max_output_tokens", worldview_max_output_tokens),
        ):
            if (
                isinstance(limit, bool)
                or not isinstance(limit, int)
                or not 1 <= limit <= 65536
            ):
                raise ValueError(f"{role} 模型 {field_name} 必须是 1 到 65536 的整数")
        config = ModelConfig(
            provider=provider,
            model=model,
            api_key=api_key,
            endpoint=endpoint,
            timeout=float(timeout),
            extra=value.get("extra") if isinstance(value.get("extra"), dict) else {},
            max_output_tokens=max_output_tokens,
            worldview_max_output_tokens=worldview_max_output_tokens,
        )
        provider_from_config(config)
        return config

    @staticmethod
    def _memory_config(value: Any, role: str) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"{role} 配置必须是对象")
        model = str(value.get("model", "")).strip()
        endpoint = str(value.get("endpoint", "")).strip().rstrip("/")
        api_key = str(value.get("api_key", ""))
        if not model or not endpoint or not api_key:
            raise ValueError(f"{role} 模型、接口地址和 API Key 都必须填写")
        protocol = str(value.get("protocol", "dashscope")).lower()
        if role == "embedding" and protocol not in {"dashscope", "openai"}:
            raise ValueError("嵌入模型协议只能是 dashscope 或 openai")
        if role == "rerank" and protocol != "dashscope":
            raise ValueError("重排序模型目前仅支持 DashScope 兼容接口")
        return {
            "model": model,
            "endpoint": endpoint,
            "api_key": api_key,
            **({"protocol": protocol} if role == "embedding" else {}),
        }

    def _validate_all(self, profiles: dict[str, Any]) -> None:
        revision = profiles.get("revision", 1) if isinstance(profiles, dict) else None
        if (
            not isinstance(profiles, dict)
            or profiles.get("version") != 1
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 1
        ):
            raise ValueError("模型配置版本无效")
        if profiles.get("bot_mode", self.bot_mode) not in {"standalone", "astrbot"}:
            raise ValueError("已保存的 Bot 运行模式无效")
        world = profiles.get("world")
        if world is not None:
            self._model_config(world, "世界")
        bot = profiles.get("bot")
        if bot is not None:
            self._model_config(bot, "Bot")
        embedding = self._memory_config(profiles.get("embedding"), "embedding")
        rerank = self._memory_config(profiles.get("rerank"), "rerank")
        if (embedding is None) != (rerank is None):
            raise ValueError("嵌入和重排序模型必须同时配置或同时关闭")
        ModelCallLimiter._validate(profiles.get("max_concurrent_calls", 2))

    def public_settings(self) -> dict[str, Any]:
        with self._lock:
            result = {
                "version": int(self._profiles["version"]),
                "revision": int(self._profiles.get("revision", 1)),
                "bot_mode": self.bot_mode,
                "bot_mode_availability": {
                    "standalone": bool(self._profiles.get("bot")),
                    "astrbot": self.service.bot_bridge is not None,
                },
                "max_concurrent_calls": self.limiter.maximum,
                "roles": {},
                "usage_support": self.usage_support_status(),
            }
            for role in ("world", "bot", "embedding", "rerank"):
                value = self._profiles.get(role)
                if value is None:
                    result["roles"][role] = None
                    continue
                if role in self.GENERATIVE_ROLES:
                    config = self._model_config(value, role)
                    result["roles"][role] = {
                        "provider": config.provider,
                        "model": config.model,
                        "endpoint": config.endpoint,
                        "timeout": config.timeout,
                        "max_output_tokens": config.max_output_tokens,
                        **(
                            {"worldview_max_output_tokens": config.worldview_max_output_tokens}
                            if role == "world"
                            else {}
                        ),
                    }
                else:
                    result["roles"][role] = {
                        key: value[key]
                        for key in ("provider", "model", "endpoint", "timeout", "protocol")
                        if key in value
                    }
                result["roles"][role]["api_key_configured"] = bool(value.get("api_key"))
                result["roles"][role]["usage_support"] = result["usage_support"][role]
            return result

    def update(self, body: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            role = str(body.get("role", "")).strip()
            if role not in {"world", "bot", "memory"}:
                raise ValueError("未知模型用途")
            incoming = body.get("config")
            if incoming is not None and not isinstance(incoming, dict):
                raise ValueError("模型配置必须是对象")
            profiles = {
                key: (dict(value) if isinstance(value, dict) else value)
                for key, value in self._profiles.items()
            }
            roles = ("embedding", "rerank") if role == "memory" else (role,)
            if incoming is None:
                for profile_role in roles:
                    profiles[profile_role] = None
            else:
                for profile_role in roles:
                    profile_update = incoming.get(profile_role) if role == "memory" else incoming
                    if profile_update is None:
                        profiles[profile_role] = None
                        continue
                    if not isinstance(profile_update, dict):
                        raise ValueError(f"{profile_role} 配置必须是对象")
                    previous = profiles.get(profile_role)
                    merged = dict(previous or {})
                    merged.update(profile_update)
                    if not str(profile_update.get("api_key", "")).strip() and not body.get("clear_api_key", False):
                        merged["api_key"] = str((previous or {}).get("api_key", ""))
                    if body.get("clear_api_key", False):
                        merged["api_key"] = ""
                    profiles[profile_role] = merged
            if "max_concurrent_calls" in body:
                profiles["max_concurrent_calls"] = body["max_concurrent_calls"]
            self._ensure_usage_support()
            usage_support = dict(self._profiles["usage_support"])
            for profile_role in roles:
                if self._profiles.get(profile_role) != profiles.get(profile_role):
                    usage_support[profile_role] = "unknown"
            profiles["usage_support"] = usage_support
            profiles["revision"] = int(profiles.get("revision", 1)) + 1
            self._validate_all(profiles)
            self.store.save(profiles)
            self._profiles = profiles
            self._apply()
            return self.public_settings()

    def switch_bot_mode(
        self,
        account: Any,
        bot_mode: str,
        *,
        confirmed: bool,
    ) -> dict[str, Any]:
        if bot_mode not in {"standalone", "astrbot"}:
            raise ValueError("Bot 模式必须是 standalone 或 astrbot")
        self.service._require_admin(account)
        if not confirmed:
            raise Conflict("切换 Bot 模式需要管理员明确确认", code="bot_mode_confirmation_required")

        with self.service._play_lock, self._lock:
            if bot_mode == self.bot_mode:
                return self.public_settings()
            worlds = self.service.list_worlds(account)
            current = next((world for world in worlds if world.current), None)
            if current is not None and current.status.value == "active":
                raise Conflict(
                    "切换 Bot 模式前必须先暂停当前世界；切换后世界保持暂停",
                    code="world_must_be_paused",
                )
            if bot_mode == "astrbot" and self.service.bot_bridge is None:
                raise Conflict("服务端没有配置 AstrBot 连接凭证", code="bot_bridge_unconfigured")
            if bot_mode == "standalone" and not isinstance(self._profiles.get("bot"), dict):
                raise Conflict("请先保存独立 Bot 模型配置", code="bot_model_unconfigured")

            previous = dict(self._profiles)
            profiles = dict(self._profiles)
            profiles["bot_mode"] = bot_mode
            profiles["revision"] = int(profiles.get("revision", 1)) + 1
            self._validate_all(profiles)
            self.store.save(profiles)
            self._profiles = profiles
            self.bot_mode = bot_mode
            self.service.bot_mode = bot_mode
            try:
                self._apply()
                with self.service.store.transaction() as conn:
                    self.service._audit_conn(
                        conn,
                        account.id,
                        "bot.mode.switch",
                        current.id if current else "service",
                        "success",
                        {
                            "from_mode": previous.get("bot_mode", "standalone"),
                            "to_mode": bot_mode,
                            "world_remains_paused": bool(current),
                        },
                    )
            except Exception:
                self.store.save(previous)
                self._profiles = previous
                self.bot_mode = str(previous.get("bot_mode", "standalone"))
                self.service.bot_mode = self.bot_mode
                self._apply()
                raise
            return self.public_settings()

    def _provider(self, role: str) -> LimitedModelProvider | None:
        value = self._profiles.get(role)
        if value is None:
            return None
        return LimitedModelProvider(
            provider_from_config(self._model_config(value, role)),
            self.limiter,
            role=role,
            usage_support_recorder=self.record_usage_support,
        )

    def _apply(self) -> None:
        world_provider = self._provider("world")
        if world_provider is None:
            self.service.simulation_engine = None
        elif self.service.simulation_engine is None:
            self.service.simulation_engine = WorldSimulationEngine(world_provider)
        else:
            self.service.simulation_engine.provider = world_provider

        if self.bot_mode == "astrbot":
            self.service.chat_provider = None
        else:
            self.service.chat_provider = self._provider("bot")

        embedding = self._memory_config(self._profiles.get("embedding"), "embedding")
        rerank = self._memory_config(self._profiles.get("rerank"), "rerank")
        if embedding and rerank:
            if self.service.memory is None:
                self.service.memory = WorldMemory(self.service.store, embedding, rerank)
            self.service.memory.reconfigure(
                embedding,
                rerank,
                call_limiter=self.limiter,
                usage_support_recorder=self.record_usage_support,
            )
        else:
            self.service.memory = None

        self.limiter.set_maximum(self._profiles.get("max_concurrent_calls", 2))
