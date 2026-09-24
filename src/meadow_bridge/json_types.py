"""Checked JSON admission for the native protocol and tool wire boundaries."""
from __future__ import annotations

import json
import math
from typing import TypeAlias

JsonValue: TypeAlias = "None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]"
JsonObject: TypeAlias = dict[str, JsonValue]


def checked_json(value: object) -> JsonValue:
    """Copy external objects into the closed, finite JSON value domain."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [checked_json(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("JSON object keys must be strings")
            result[key] = checked_json(item)
        return result
    raise ValueError("Expected a finite JSON value")


def parse_json(text: str | bytes) -> JsonValue:
    """Decode JSON without letting dynamic decoder values escape admission."""
    value: object = json.loads(text)
    return checked_json(value)


def json_object(value: JsonValue, description: str = "value") -> JsonObject:
    """Require an object at a documented wire position."""
    if not isinstance(value, dict):
        raise ValueError(f"{description} must be an object")
    return value


def json_text(value: JsonValue) -> str:
    """Encode bounded evidence without losing escaped Unicode code points."""
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def required_string(value: JsonObject, key: str) -> str:
    """Admit a nonempty protocol string without coercion."""
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise ValueError(f"{key} must be a nonempty string")
    return result
