"""独立模式模型供应商适配。

供应商调用统一返回实际用量；上游没有返回 usage 时，`usage_supported` 为
False，调用方不能拿估算值冒充实际 Token。密钥只存在进程内的配置对象，
不写入世界 JSON、日志或客户端消息。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from urllib.parse import urlsplit, quote


class ModelProviderError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, usage_supported: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.usage_supported = usage_supported


@dataclass(slots=True)
class ModelConfig:
    provider: str
    model: str
    api_key: str = ""
    endpoint: str = ""
    timeout: float = 60.0
    extra: dict[str, Any] = field(default_factory=dict)
    max_output_tokens: int | None = None
    worldview_max_output_tokens: int | None = None


@dataclass(slots=True)
class ModelResponse:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    model: str
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def usage_supported(self) -> bool:
        return self.input_tokens is not None and self.output_tokens is not None

    @property
    def total_tokens(self) -> int | None:
        if not self.usage_supported:
            return None
        return int(self.input_tokens or 0) + int(self.output_tokens or 0)


class ModelProvider(Protocol):
    config: ModelConfig

    def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 1024, temperature: float = 0.7) -> ModelResponse:
        ...


def _usage(value: Any) -> tuple[int | None, int | None]:
    if not isinstance(value, dict):
        return None, None
    input_tokens = value.get("prompt_tokens", value.get("input_tokens"))
    output_tokens = value.get("completion_tokens", value.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None, None
    try:
        return int(input_tokens or 0), int(output_tokens or 0)
    except (TypeError, ValueError):
        return None, None


class _HttpProvider:
    def __init__(self, config: ModelConfig) -> None:
        self.config = config

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(url, data=raw, headers={"Content-Type": "application/json", **headers}, method="POST")
        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            retryable = exc.code in (408, 409, 429) or exc.code >= 500
            raise ModelProviderError(f"模型服务返回 HTTP {exc.code}", retryable=retryable) from exc
        except (URLError, TimeoutError, OSError) as exc:
            # Some transports (notably http.client.RemoteDisconnected) escape as
            # OSError rather than URLError. Keep the detail generic: URL-based
            # providers may put credentials in query parameters.
            raise ModelProviderError(
                f"模型服务连接失败（{type(exc).__name__}）",
                retryable=True,
            ) from exc
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelProviderError("模型服务返回了无效 JSON", retryable=True) from exc
        if not isinstance(value, dict):
            raise ModelProviderError("模型服务返回格式不是对象")
        return value


class OpenAICompatibleProvider(_HttpProvider):
    def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 1024, temperature: float = 0.7) -> ModelResponse:
        endpoint = self.config.endpoint.rstrip("/") or "https://api.openai.com/v1"
        if not endpoint.endswith("/chat/completions"):
            endpoint += "/chat/completions"
        value = self._post(endpoint, {"model": self.config.model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}, {"Authorization": f"Bearer {self.config.api_key}"} if self.config.api_key else {})
        try:
            text = value["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError("OpenAI 兼容接口缺少 choices.message.content") from exc
        input_tokens, output_tokens = _usage(value.get("usage"))
        return ModelResponse(str(text), input_tokens, output_tokens, str(value.get("model", self.config.model)), value)


class AnthropicProvider(_HttpProvider):
    def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 1024, temperature: float = 0.7) -> ModelResponse:
        endpoint = self.config.endpoint.rstrip("/") or "https://api.anthropic.com/v1/messages"
        if not endpoint.endswith("/messages"):
            endpoint += "/messages"
        system = "\n".join(item["content"] for item in messages if item.get("role") == "system")
        turns = [{"role": item["role"], "content": item["content"]} for item in messages if item.get("role") != "system"]
        payload: dict[str, Any] = {"model": self.config.model, "messages": turns, "max_tokens": max_tokens, "temperature": temperature}
        if system:
            payload["system"] = system
        value = self._post(endpoint, payload, {"x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"})
        try:
            text = "".join(str(item.get("text", "")) for item in value["content"] if item.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise ModelProviderError("Anthropic 接口缺少 content.text") from exc
        input_tokens, output_tokens = _usage(value.get("usage"))
        return ModelResponse(text, input_tokens, output_tokens, str(value.get("model", self.config.model)), value)


class GeminiProvider(_HttpProvider):
    def complete(self, messages: list[dict[str, str]], *, max_tokens: int = 1024, temperature: float = 0.7) -> ModelResponse:
        return self._complete(messages, max_tokens=max_tokens, temperature=temperature)

    def complete_structured(
        self,
        messages: list[dict[str, str]],
        *,
        response_schema: dict[str, Any],
        max_tokens: int = 1024,
        temperature: float = 0.7,
    ) -> ModelResponse:
        return self._complete(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_schema=response_schema,
        )

    def _complete(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelResponse:
        endpoint = self.config.endpoint.rstrip("/") or "https://generativelanguage.googleapis.com/v1beta"
        # AstrBot 的 Gemini SDK 会在供应商 base_url 后自动使用 v1beta；
        # 配置中的 api_base 通常只有域名，因此独立服务端也要补上该路径。
        if not urlsplit(endpoint).path.strip("/"):
            endpoint += "/v1beta"
        if ":generateContent" not in endpoint:
            endpoint += f"/models/{self.config.model}:generateContent"
        contents = [{"role": "model" if item["role"] == "assistant" else "user", "parts": [{"text": item["content"]}]} for item in messages if item["role"] != "system"]
        system = [item["content"] for item in messages if item["role"] == "system"]
        generation_config: dict[str, Any] = {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        }
        if response_schema is None:
            generation_config["responseMimeType"] = "application/json"
        else:
            generation_config["responseFormat"] = {
                "text": {
                    "mimeType": "application/json",
                    "schema": response_schema,
                }
            }
        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": generation_config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": "\n".join(system)}]}
        separator = "&" if "?" in endpoint else "?"
        value = self._post(f"{endpoint}{separator}key={quote(self.config.api_key, safe='')}", payload, {})
        try:
            text = "".join(str(part["text"]) for part in value["candidates"][0]["content"]["parts"] if "text" in part and not part.get("thought"))
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelProviderError("Gemini 接口缺少 candidates.content.parts.text") from exc
        metadata = value.get("usageMetadata") or {}
        input_tokens, output_tokens = _usage({"prompt_tokens": metadata.get("promptTokenCount"), "completion_tokens": metadata.get("candidatesTokenCount")})
        # Gemini total includes reasoning tokens omitted from candidatesTokenCount.
        if input_tokens is not None and metadata.get("totalTokenCount") is not None:
            output_tokens = max(0, int(metadata["totalTokenCount"]) - input_tokens)
        return ModelResponse(str(text), input_tokens, output_tokens, self.config.model, value)


def provider_from_config(config: ModelConfig) -> ModelProvider:
    name = config.provider.lower().replace("_", "-")
    if name in {"openai", "openai-compatible", "custom", "compatible"}:
        return OpenAICompatibleProvider(config)
    if name in {"anthropic", "claude"}:
        return AnthropicProvider(config)
    if name in {"gemini", "google", "google-gemini"}:
        return GeminiProvider(config)
    raise ValueError(f"不支持的模型供应商：{config.provider}")
