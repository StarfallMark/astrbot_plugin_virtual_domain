"""从独立配置文件或 AstrBot 配置中加载世界模型供应商。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .providers import ModelConfig, ModelProvider, provider_from_config


def _first(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def load_model_provider(path: str | Path, *, model_id: str | None = None) -> ModelProvider:
    """加载简化模型配置或 AstrBot 的 ``cmd_config.json``。

    简化格式为 ``provider/model/api_key/endpoint``；AstrBot 格式会从
    ``provider_sources`` 和 ``provider`` 中选取指定模型。密钥只保存在当前
    服务端进程的配置对象中，不写入世界存档。
    """

    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("模型配置必须是 JSON 对象")
    if data.get("provider") and data.get("model"):
        return provider_from_config(
            ModelConfig(
                provider=str(data["provider"]),
                model=str(data["model"]),
                api_key=str(data.get("api_key", "")),
                endpoint=str(data.get("endpoint", "")),
                timeout=float(data.get("timeout", 120)),
                extra=dict(data.get("extra") or {}),
            )
        )

    sources = {str(item.get("id")): item for item in data.get("provider_sources", []) if isinstance(item, dict)}
    providers = [item for item in data.get("provider", []) if isinstance(item, dict)]
    if not providers:
        raise ValueError("配置中没有 provider")
    selected = None
    if model_id:
        selected = next((item for item in providers if item.get("id") == model_id or item.get("model") == model_id), None)
    if selected is None:
        selected = next((item for item in providers if str(item.get("id", "")).startswith("gemini/")), providers[0])
    identifier = str(selected.get("id") or selected.get("model") or "")
    provider_name = identifier.split("/", 1)[0] if "/" in identifier else str(selected.get("provider") or "openai-compatible")
    source = sources.get(provider_name, {})
    endpoint = str(selected.get("api_base") or source.get("api_base") or "")
    key = _first(selected.get("key") or source.get("key"))
    model = str(selected.get("model") or identifier.split("/", 1)[-1])
    return provider_from_config(
        ModelConfig(
            provider=provider_name,
            model=model,
            api_key=key,
            endpoint=endpoint,
            timeout=float(selected.get("timeout") or source.get("timeout") or 120),
        )
    )

