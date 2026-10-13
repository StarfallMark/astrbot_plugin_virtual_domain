"""Helpers for reading actual token usage from AstrBot/provider responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_TOTAL_KEYS = (
    "total_tokens",
    "totalTokens",
    "total_token",
    "totalToken",
    "total",
)
_PROMPT_KEYS = ("prompt_tokens", "promptTokenCount", "input_tokens", "inputTokenCount")
_COMPLETION_KEYS = (
    "completion_tokens",
    "candidatesTokenCount",
    "output_tokens",
    "outputTokenCount",
)
_TRACE_INPUT_KEYS = ("input_other", "input_cached")
_TRACE_OUTPUT_KEYS = ("output",)


def _read(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return parsed if parsed > 0 else 0


def _first_positive(value: Any, keys: tuple[str, ...]) -> int:
    for key in keys:
        count = _positive_int(_read(value, key))
        if count:
            return count
    return 0


def extract_actual_token_count(usage: Any) -> int:
    """Extract an actual total, or derive one from recognized input/output fields.

    AstrBot, OpenAI-compatible APIs, and Google responses use different field
    names. Unknown shapes return zero; this helper never estimates tokens from
    text length.
    """

    if usage is None:
        return 0
    total = _first_positive(usage, _TOTAL_KEYS)
    if total:
        return total

    trace_input = sum(_positive_int(_read(usage, key)) for key in _TRACE_INPUT_KEYS)
    trace_output = sum(_positive_int(_read(usage, key)) for key in _TRACE_OUTPUT_KEYS)
    if trace_input or trace_output:
        return trace_input + trace_output

    prompt = _first_positive(usage, _PROMPT_KEYS)
    completion = _first_positive(usage, _COMPLETION_KEYS)
    if prompt or completion:
        return prompt + completion
    return 0
