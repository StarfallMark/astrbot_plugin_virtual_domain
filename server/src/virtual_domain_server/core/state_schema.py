"""Validation for administrator-declared world state attributes."""

from __future__ import annotations

import json
import math
import re
from typing import Any


MAX_SCHEMA_FIELDS = 128
MAX_SCHEMA_BYTES = 32_768
MAX_STATE_VALUE_BYTES = 65_536
MAX_STATE_DEPTH = 16
MAX_STATE_ITEMS = 10_000
RESERVED_STATE_KEYS = frozenset({"event_count", "last_event", "memory_entries"})
_FIELD_NAME = re.compile(r"^[A-Za-z0-9_\-\u0080-\uffff]{1,80}$")
_TYPES = frozenset({"string", "number", "integer", "boolean", "object", "array", "null"})
_MAX_SCHEMA_RULES = 1_024


def validate_state_schema(schema: Any) -> dict[str, dict[str, Any]]:
    """Return a normalized schema or raise ``ValueError``.

    A world schema is a mapping from top-level state attribute names to a
    compact JSON Schema subset. Object ``properties`` and array ``items`` may
    recursively declare the same rules. Unknown attributes are rejected
    whenever a non-empty schema is configured.
    """

    if schema is None:
        return {}
    if not isinstance(schema, dict):
        raise ValueError("state_schema 必须是 JSON 对象")
    if len(schema) > MAX_SCHEMA_FIELDS:
        raise ValueError(f"state_schema 最多允许 {MAX_SCHEMA_FIELDS} 个属性")
    try:
        encoded = json.dumps(schema, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("state_schema 必须只包含有效 JSON 值") from exc
    if len(encoded.encode("utf-8")) > MAX_SCHEMA_BYTES:
        raise ValueError(f"state_schema 不能超过 {MAX_SCHEMA_BYTES} 字节")

    normalized: dict[str, dict[str, Any]] = {}
    rule_count = [0]
    for name, definition in schema.items():
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
            raise ValueError("state_schema 属性名只能包含字母、数字、下划线、连字符或 Unicode 字符，长度不超过 80")
        if name in RESERVED_STATE_KEYS:
            raise ValueError(f"{name} 是服务端维护字段，不能声明为世界属性")
        normalized[name] = _normalize_rule(
            definition,
            f"state_schema.{name}",
            depth=0,
            rule_count=rule_count,
        )
    return normalized


def _normalize_rule(
    definition: Any,
    path: str,
    *,
    depth: int,
    rule_count: list[int],
) -> dict[str, Any]:
    if not isinstance(definition, dict):
        raise ValueError(f"{path} 必须是对象")
    if depth > MAX_STATE_DEPTH:
        raise ValueError(f"{path} 嵌套超过 {MAX_STATE_DEPTH} 层")
    rule_count[0] += 1
    if rule_count[0] > _MAX_SCHEMA_RULES:
        raise ValueError(f"state_schema 最多允许 {_MAX_SCHEMA_RULES} 条规则")

    value_type = definition.get("type")
    if not isinstance(value_type, str) or value_type not in _TYPES:
        raise ValueError(f"{path}.type 必须是 {', '.join(sorted(_TYPES))}")
    allowed = {"type", "minimum", "maximum", "min_length", "max_length", "enum"}
    if value_type == "object":
        allowed.update({"properties", "required", "additional_properties"})
    elif value_type == "array":
        allowed.add("items")
    unknown = set(definition) - allowed
    if unknown:
        raise ValueError(f"{path} 含有不支持的规则：{', '.join(sorted(map(str, unknown)))}")

    rule = {key: value for key, value in definition.items() if key not in {"properties", "items"}}
    for bound_name in ("minimum", "maximum"):
        if bound_name in rule:
            bound = rule[bound_name]
            if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not math.isfinite(bound):
                raise ValueError(f"{path}.{bound_name} 必须是有限数值")
    if "minimum" in rule and "maximum" in rule and rule["minimum"] > rule["maximum"]:
        raise ValueError(f"{path}.minimum 不能大于 maximum")
    if value_type not in {"number", "integer"} and ({"minimum", "maximum"} & rule.keys()):
        raise ValueError(f"{path} 只有 number 或 integer 类型可以设置数值范围")

    for bound_name in ("min_length", "max_length"):
        if bound_name in rule:
            bound = rule[bound_name]
            if isinstance(bound, bool) or not isinstance(bound, int) or not 0 <= bound <= MAX_STATE_VALUE_BYTES:
                raise ValueError(f"{path}.{bound_name} 必须是 0 至 {MAX_STATE_VALUE_BYTES} 的整数")
    if "min_length" in rule and "max_length" in rule and rule["min_length"] > rule["max_length"]:
        raise ValueError(f"{path}.min_length 不能大于 max_length")
    if value_type not in {"string", "array", "object"} and ({"min_length", "max_length"} & rule.keys()):
        raise ValueError(f"{path} 的类型不支持长度限制")

    if value_type == "object":
        raw_properties = definition.get("properties")
        if "properties" in definition:
            if not isinstance(raw_properties, dict):
                raise ValueError(f"{path}.properties 必须是对象")
            if len(raw_properties) > MAX_SCHEMA_FIELDS:
                raise ValueError(f"{path}.properties 最多允许 {MAX_SCHEMA_FIELDS} 个属性")
            properties: dict[str, dict[str, Any]] = {}
            for name, child in raw_properties.items():
                if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
                    raise ValueError(f"{path}.properties 属性名格式无效")
                properties[name] = _normalize_rule(
                    child,
                    f"{path}.properties.{name}",
                    depth=depth + 1,
                    rule_count=rule_count,
                )
            rule["properties"] = properties
        if "required" in definition:
            required = definition["required"]
            if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
                raise ValueError(f"{path}.required 必须是文本数组")
            if len(required) != len(set(required)):
                raise ValueError(f"{path}.required 不能包含重复属性")
            properties = rule.get("properties", {})
            if any(name not in properties for name in required):
                raise ValueError(f"{path}.required 只能引用已声明的 properties")
            rule["required"] = list(required)
        additional = definition.get("additional_properties", "properties" not in definition)
        if not isinstance(additional, bool):
            raise ValueError(f"{path}.additional_properties 必须是布尔值")
        rule["additional_properties"] = additional
    elif value_type == "array" and "items" in definition:
        rule["items"] = _normalize_rule(
            definition["items"],
            f"{path}.items",
            depth=depth + 1,
            rule_count=rule_count,
        )

    if "enum" in rule:
        choices = rule["enum"]
        if not isinstance(choices, list) or not choices or len(choices) > 256:
            raise ValueError(f"{path}.enum 必须是含 1 至 256 项的数组")
        try:
            for choice in choices:
                _validate_json_value(choice, f"{path}.enum", max_depth=MAX_STATE_DEPTH)
                _validate_value(f"{path}.enum", choice, {key: item for key, item in rule.items() if key != "enum"})
        except ValueError as exc:
            raise ValueError(f"{path}.enum {exc}") from exc
    return rule


def validate_state_values(
    schema: Any,
    values: Any,
    *,
    allow_reserved: bool = True,
    require_declared: bool | None = None,
) -> None:
    """Validate values against a world schema.

    Empty schemas retain legacy free-form state. A configured schema closes the
    top-level attribute set by default. Service-owned fields are skipped when
    checking a live state snapshot, but are never accepted as model patches.
    """

    normalized = validate_state_schema(schema)
    if not isinstance(values, dict):
        raise ValueError("世界状态必须是 JSON 对象")
    require_declared = bool(normalized) if require_declared is None else require_declared
    try:
        encoded = json.dumps(values, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("世界状态必须只包含有效 JSON 值") from exc
    if len(encoded.encode("utf-8")) > MAX_STATE_VALUE_BYTES:
        raise ValueError(f"世界状态不能超过 {MAX_STATE_VALUE_BYTES} 字节")
    _validate_json_value(values, "世界状态", max_depth=MAX_STATE_DEPTH)

    for name, value in values.items():
        if name in RESERVED_STATE_KEYS:
            if allow_reserved:
                continue
            raise ValueError(f"{name} 是服务端维护字段，不能作为初始世界属性")
        rule = normalized.get(name)
        if rule is None:
            if require_declared:
                raise ValueError(f"世界状态属性 {name} 未在 state_schema 中声明")
            continue
        _validate_value(name, value, rule)


def validate_state_patch(schema: Any, patch: Any) -> None:
    """Reject model patches that alter server metadata or violate the schema."""

    if not isinstance(patch, dict):
        raise ValueError("state_patch 必须是 JSON 对象")
    for key in patch:
        if key in RESERVED_STATE_KEYS:
            raise ValueError(f"state_patch 不能修改服务端维护字段 {key}")
    validate_state_values(schema, patch, allow_reserved=False)


def _validate_value(name: str, value: Any, rule: dict[str, Any]) -> None:
    value_type = rule["type"]
    valid = {
        "string": isinstance(value, str),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "null": value is None,
    }[value_type]
    if not valid:
        raise ValueError(f"世界状态属性 {name} 必须是 {value_type}")
    if value_type in {"number", "integer"}:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"世界状态属性 {name} 必须是有限数值")
        if "minimum" in rule and value < rule["minimum"]:
            raise ValueError(f"世界状态属性 {name} 不能小于 {rule['minimum']}")
        if "maximum" in rule and value > rule["maximum"]:
            raise ValueError(f"世界状态属性 {name} 不能大于 {rule['maximum']}")
    if value_type in {"string", "array", "object"}:
        length = len(value)
        if "min_length" in rule and length < rule["min_length"]:
            raise ValueError(f"世界状态属性 {name} 长度不能小于 {rule['min_length']}")
        if "max_length" in rule and length > rule["max_length"]:
            raise ValueError(f"世界状态属性 {name} 长度不能大于 {rule['max_length']}")
    if value_type == "object":
        properties = rule.get("properties", {})
        required = rule.get("required", [])
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"世界状态属性 {name} 缺少必需字段：{', '.join(missing)}")
        for key, child in value.items():
            child_rule = properties.get(key)
            if child_rule is None:
                if not rule.get("additional_properties", "properties" not in rule):
                    raise ValueError(f"世界状态属性 {name}.{key} 未在 Schema 中声明")
                continue
            _validate_value(f"{name}.{key}", child, child_rule)
    if value_type == "array" and "items" in rule:
        for index, child in enumerate(value):
            _validate_value(f"{name}[{index}]", child, rule["items"])
    if "enum" in rule and value not in rule["enum"]:
        raise ValueError(f"世界状态属性 {name} 不在允许值范围内")


def _validate_json_value(value: Any, path: str, *, max_depth: int) -> None:
    count = 0

    def visit(item: Any, current_path: str, depth: int) -> None:
        nonlocal count
        count += 1
        if count > MAX_STATE_ITEMS:
            raise ValueError(f"{path} 的项目数量超过 {MAX_STATE_ITEMS}")
        if depth > max_depth:
            raise ValueError(f"{path} 嵌套超过 {max_depth} 层")
        if item is None or isinstance(item, (str, bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"{current_path} 必须是有限数值")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{current_path}[{index}]", depth + 1)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"{current_path} 的对象键必须是文本")
                visit(child, f"{current_path}.{key}", depth + 1)
            return
        raise ValueError(f"{current_path} 包含不支持的 JSON 值")

    visit(value, path, 0)
