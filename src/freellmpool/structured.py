"""G11 structured-output repair loop: validate JSON-mode replies, repair once.

Bound: at most MAX_REPAIRS follow-up turns per request, each one fully
accounted through the normal reserve/settle path, then an honest error.
Parsing is strict raw JSON — fenced or prose-wrapped output triggers a
repair turn carrying the validation error, never silent salvage.
"""

from __future__ import annotations

import json
from typing import Any

MAX_REPAIRS = 1
_MAX_ERROR_CHARS = 400


def parse_json_output(text: str) -> tuple[Any | None, str | None]:
    """Parse strict raw JSON; return (value, None) or (None, bounded error)."""
    try:
        return json.loads(text.strip()), None
    except (ValueError, TypeError) as exc:
        detail = str(exc)
        if len(detail) > _MAX_ERROR_CHARS:
            detail = detail[:_MAX_ERROR_CHARS] + "…"
        excerpt = text.strip().replace("\n", " ")[:80]
        return None, f"response was not valid JSON ({detail}); began with: {excerpt!r}"


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def validate_schema(value: Any, schema: Any, *, path: str = "$") -> list[str]:
    """Validate against a JSON-schema subset: type/properties/required/items/enum/additionalProperties."""
    if not isinstance(schema, dict):
        return []
    errors: list[str] = []
    expected = schema.get("type")
    if isinstance(expected, str):
        actual = _type_name(value)
        if expected == "number" and actual == "integer":
            actual = "number"
        if actual != expected:
            return [f"{path}: expected {expected}"]
    if "enum" in schema and value not in schema["enum"]:
        allowed = json.dumps(schema["enum"])
        errors.append(f"{path}: not one of {allowed}")
    if isinstance(value, dict):
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        properties = schema.get("properties") or {}
        for name, item in value.items():
            if name in properties:
                errors.extend(validate_schema(item, properties[name], path=f"{path}.{name}"))
            elif schema.get("additionalProperties") is False:
                errors.append(f"{path}: unexpected property {name!r}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            errors.extend(validate_schema(item, schema["items"], path=f"{path}[{index}]"))
    return errors


def wants_json(response_format: Any) -> tuple[bool, dict[str, Any] | None]:
    """Interpret an OpenAI response_format; return (json_requested, schema_or_None)."""
    if response_format is None:
        return False, None
    if not isinstance(response_format, dict):
        raise ValueError(f"unsupported response_format: {response_format!r}")
    kind = response_format.get("type")
    if kind == "json_object":
        return True, None
    if kind == "json_schema":
        spec = response_format.get("json_schema") or {}
        schema = spec.get("schema")
        if not isinstance(schema, dict):
            raise ValueError("json_schema response_format is missing its schema object")
        return True, schema
    if kind == "text":
        return False, None
    raise ValueError(f"unsupported response_format type: {kind!r}")


def check_reply(text: str, response_format: Any) -> tuple[Any | None, str | None]:
    """Validate reply text; return (parsed_value, None) or (None, error)."""
    wanted, schema = wants_json(response_format)
    if not wanted:
        return None, None
    value, error = parse_json_output(text)
    if error is not None:
        return None, error
    assert schema is None or isinstance(schema, dict)
    violations = validate_schema(value, schema) if schema is not None else []
    if violations:
        return None, "JSON failed schema validation: " + "; ".join(violations[:5])
    return value, None


def repair_messages(messages: list[Any], bad_text: str, error: str) -> list[Any]:
    """Append the bad output plus a user turn carrying the validation error."""
    return [
        *messages,
        {"role": "assistant", "content": bad_text},
        {"role": "user", "content": (
            "That response was invalid for the requested JSON output. "
            f"Validation error: {error} "
            "Reply with exactly one raw JSON value and nothing else — "
            "no prose, no markdown fences, no explanation."
        )},
    ]
