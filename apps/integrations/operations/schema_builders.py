"""Shared OAuth and JSON Schema construction for Operations MCP tools.

Domain catalogs use these builders to keep approval fields, annotations and
consent scopes consistent without depending on the assembled tool catalog.
"""

from copy import deepcopy

from apps.integrations.operations_auth import (
    OFFLINE_SCOPE,
    OPERATIONS_READ_SCOPE,
    OPERATIONS_WRITE_SCOPE,
)


OAUTH_READ_SCHEMES = [
    {
        "type": "oauth2",
        "scopes": [
            OPERATIONS_READ_SCOPE,
            OFFLINE_SCOPE,
        ],
    }
]

OAUTH_WRITE_SCHEMES = [
    {
        "type": "oauth2",
        "scopes": [
            OPERATIONS_READ_SCOPE,
            OPERATIONS_WRITE_SCOPE,
            OFFLINE_SCOPE,
        ],
    }
]


def _write_properties(extra=None):
    properties = {
        "dry_run": {
            "type": "boolean",
            "default": True,
            "description": "Preview the exact change without applying it.",
        },
        "approved": {
            "type": "boolean",
            "default": False,
            "description": "Set true only after the human approved an approval-required dry-run.",
        },
        "approval_event_id": {
            "type": "string",
            "format": "uuid",
            "description": (
                "Immutable dry-run audit event ID returned by SHVYA. Required "
                "with approved=true when the dry-run said approval_required=true."
            ),
        },
        "reason": {
            "type": "string",
            "minLength": 8,
            "maxLength": 500,
            "description": "Specific operational reason for the proposed mutation.",
        },
    }
    properties.update(extra or {})
    return properties


def _tool(
    name,
    title,
    description,
    properties=None,
    required=None,
    *,
    read_only=True,
    requires_write_scope=None,
    destructive=False,
):
    security_schemes = (
        OAUTH_WRITE_SCHEMES
        if (
            (not read_only)
            if requires_write_scope is None
            else requires_write_scope
        )
        else OAUTH_READ_SCHEMES
    )
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties or {},
            "required": required or [],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": read_only,
            "destructiveHint": destructive,
            "openWorldHint": False,
        },
        "securitySchemes": security_schemes,
        "_meta": {
            "securitySchemes": deepcopy(security_schemes),
        },
    }
