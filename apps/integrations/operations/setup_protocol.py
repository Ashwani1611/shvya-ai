"""Native MCP prompt/resource access through the setup-library boundary."""

import json

from apps.integrations.mcp_schema import MCPInputValidationError, validate_mcp_arguments
from apps.integrations.operations import setup_library
from apps.integrations.operations_policy import CAP_SETUP_LIBRARY_READ
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _reject_secret_like_content,
)


SETUP_PROTOCOL_METHODS = frozenset({
    "prompts/list", "prompts/get", "resources/list", "resources/read",
})
SETUP_LIBRARY_TOOL_NAMES = frozenset({
    "list_setup_library", "get_setup_library_resource", "get_setup_variable_schema",
})
_META = {"type": "object"}
_LIST_PROPERTIES = {"cursor": {"type": "string", "maxLength": 200}, "_meta": _META}
_PARAMETER_SCHEMAS = {
    "prompts/list": {"properties": _LIST_PROPERTIES},
    "resources/list": {"properties": _LIST_PROPERTIES},
    "prompts/get": {
        "properties": {
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "arguments": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "organization_name": {"type": "string", "maxLength": 4000},
                    "task": {"type": "string", "maxLength": 4000},
                },
            },
            "_meta": _META,
        },
        "required": ["name"],
    },
    "resources/read": {
        "properties": {
            "uri": {"type": "string", "minLength": 1, "maxLength": 300},
            "offset": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20000, "default": 8000},
            "_meta": _META,
        },
        "required": ["uri"],
    },
}


class SetupResourceNotFound(OperationsToolError):
    code = "operations_setup_resource_not_found"


def execute_setup_protocol(*, identity, method, params):
    """Return trusted guidance with separate untrusted, secret-screened context."""
    # Import lazily to keep the existing Operations registry/transport boundary
    # acyclic. The same guard owns native and tools/call library access.
    from apps.integrations.operations.tools.setup_authoring import library_access

    organization = library_access(identity)
    if method not in SETUP_PROTOCOL_METHODS:
        raise OperationsToolError("Unknown setup protocol method.")
    schema = {"type": "object", "additionalProperties": False, **_PARAMETER_SCHEMAS[method]}
    try:
        validate_mcp_arguments(params, schema)
        if len(json.dumps(params.get("_meta", {}), ensure_ascii=False)) > 4096:
            raise MCPInputValidationError("Setup request metadata is too large.")
    except MCPInputValidationError as exc:
        raise OperationsToolError(str(exc)) from exc

    try:
        if method == "prompts/list":
            data = setup_library.list_prompts(params.get("cursor"))
            summary = {"operation": method, "prompt_count": len(data["prompts"])}
        elif method == "resources/list":
            data = setup_library.list_resources(params.get("cursor"))
            summary = {"operation": method, "resource_count": len(data["resources"])}
        elif method == "prompts/get":
            arguments = params.get("arguments", {})
            _reject_secret_like_content(arguments, field="setup_prompt_context")
            data = setup_library.get_prompt(params["name"], arguments)
            summary = {"operation": method, "prompt_name": params["name"], "context_field_count": len(arguments)}
        else:
            if not any(entry["uri"] == params["uri"] for entry in setup_library.library_entries()):
                raise SetupResourceNotFound("Unknown setup resource.")
            data = setup_library.read_resource(
                params["uri"], offset=params.get("offset", 0), limit=params.get("limit", 8000),
            )
            summary = {
                "operation": method, "resource_uri": params["uri"],
                "offset": data["_meta"]["offset"],
                "returned_chars": len(data["contents"][0]["text"]),
                "truncated": data["_meta"]["truncated"],
            }
    except ValueError as exc:
        if isinstance(exc, OperationsToolError):
            raise
        raise OperationsToolError(str(exc)) from exc

    return ToolExecution(
        data=data,
        capability=CAP_SETUP_LIBRARY_READ,
        target_type="organization" if organization else "platform",
        target_id=str(organization.id) if organization else "",
        audit_summary=summary,
    )
