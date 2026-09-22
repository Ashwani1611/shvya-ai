"""Small server-side validator for SHVYA MCP tool argument schemas.

The MCP tool catalog already publishes JSON-Schema-like input contracts. This
module enforces the subset SHVYA uses at the HTTP boundary so clients cannot
bypass additionalProperties/required/type/range/enum/format constraints by
calling a handler directly with extra JSON fields.
"""

from __future__ import annotations

import re
import uuid


class MCPInputValidationError(ValueError):
    pass


def _fail(path: str, message: str):
    raise MCPInputValidationError(f"{path}: {message}")


def _validate(value, schema, *, path: str):
    if not isinstance(schema, dict):
        return

    expected = schema.get("type")
    if isinstance(expected, list):
        def _matches(candidate):
            if candidate == "object":
                return isinstance(value, dict)
            if candidate == "array":
                return isinstance(value, list)
            if candidate == "string":
                return isinstance(value, str)
            if candidate == "integer":
                return isinstance(value, int) and not isinstance(value, bool)
            if candidate == "number":
                return isinstance(value, (int, float)) and not isinstance(value, bool)
            if candidate == "boolean":
                return isinstance(value, bool)
            if candidate == "null":
                return value is None
            return False

        if not any(_matches(candidate) for candidate in expected):
            _fail(path, "has an invalid type")
        if isinstance(value, dict) and "object" in expected:
            expected = "object"
        elif isinstance(value, list) and "array" in expected:
            expected = "array"
        elif isinstance(value, bool) and "boolean" in expected:
            expected = "boolean"
        elif isinstance(value, str) and "string" in expected:
            expected = "string"
        elif isinstance(value, int) and not isinstance(value, bool) and "integer" in expected:
            expected = "integer"
        elif isinstance(value, (int, float)) and not isinstance(value, bool) and "number" in expected:
            expected = "number"
        elif value is None and "null" in expected:
            return

    if expected == "object":
        if not isinstance(value, dict):
            _fail(path, "must be an object")
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for key in required:
            if key not in value:
                _fail(path, f"missing required field '{key}'")
        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                _fail(path, "unexpected field(s): " + ", ".join(unknown))
        for key, item in value.items():
            child = properties.get(key)
            if isinstance(child, dict):
                _validate(item, child, path=f"{path}.{key}")

    elif expected == "array":
        if not isinstance(value, list):
            _fail(path, "must be an array")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, path=f"{path}[{index}]")

    elif expected == "string":
        if not isinstance(value, str):
            _fail(path, "must be a string")
        minimum = schema.get("minLength")
        maximum = schema.get("maxLength")
        if minimum is not None and len(value) < int(minimum):
            _fail(path, f"must contain at least {int(minimum)} characters")
        if maximum is not None and len(value) > int(maximum):
            _fail(path, f"must contain at most {int(maximum)} characters")
        if schema.get("format") == "uuid":
            try:
                uuid.UUID(value)
            except (TypeError, ValueError, AttributeError) as exc:
                raise MCPInputValidationError(f"{path}: must be a valid UUID") from exc
        pattern = schema.get("pattern")
        if pattern and re.fullmatch(str(pattern), value) is None:
            _fail(path, "has an invalid format")

    elif expected == "integer":
        if not isinstance(value, int) or isinstance(value, bool):
            _fail(path, "must be an integer")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < int(minimum):
            _fail(path, f"must be >= {int(minimum)}")
        if maximum is not None and value > int(maximum):
            _fail(path, f"must be <= {int(maximum)}")

    elif expected == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            _fail(path, "must be a number")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if minimum is not None and value < float(minimum):
            _fail(path, f"must be >= {minimum}")
        if maximum is not None and value > float(maximum):
            _fail(path, f"must be <= {maximum}")

    elif expected == "boolean":
        if not isinstance(value, bool):
            _fail(path, "must be true or false")

    enum = schema.get("enum")
    if enum is not None and value not in enum:
        _fail(path, "must be one of: " + ", ".join(str(item) for item in enum))


def validate_mcp_arguments(arguments, schema):
    if not isinstance(arguments, dict):
        raise MCPInputValidationError("arguments: must be an object")
    _validate(arguments, schema or {"type": "object"}, path="arguments")
