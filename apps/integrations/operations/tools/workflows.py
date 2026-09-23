"""Workflow schema and validation tools for SHVYA Operations MCP."""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.integrations.operations_policy import CAP_ORGANIZATION_READ
from services.triggers.rules import (
    catalog as workflow_catalog,
    validate as validate_workflow_rule,
)
from apps.integrations.operations_tools import (
    OperationsToolError,
    ToolExecution,
    _assert_workflow_safe_attribute_references,
    _organization_for,
    _reject_secret_like_content,
    _require_operations_capability,
    _sensitive_attribute_keys,
)

def _workflow_catalog_safe(organization):
    data = workflow_catalog(organization)
    sensitive = _sensitive_attribute_keys(organization)
    data["attributes"] = [
        item for item in data.get("attributes", [])
        if item.get("key") not in sensitive
    ]
    return data


def _trigger_schema(trigger_type):
    properties = {
        "scopes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "pipeline": {"type": "string", "format": "uuid"},
                    "stages": {
                        "type": "array",
                        "items": {"type": "string", "format": "uuid"},
                        "minItems": 1,
                    },
                },
                "required": ["pipeline", "stages"],
                "additionalProperties": False,
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
        "attributes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "match": {"type": "string", "enum": ["equals", "contains"]},
                    "values": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                },
                "required": ["key", "match", "values"],
                "additionalProperties": False,
            },
        },
    }
    required = [] if trigger_type == "sequence_ended" else ["scopes"]
    if trigger_type in {"no_response", "stage_idle"}:
        properties.update(
            {
                "duration": {"type": "integer", "minimum": 1, "maximum": 525600},
                "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
            }
        )
        required += ["duration", "unit"]
    elif trigger_type == "keyword":
        properties["keywords"] = {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 50,
        }
        required += ["keywords"]
    elif trigger_type == "sequence_ended":
        properties["sequences"] = {
            "type": "array",
            "items": {"type": "string", "format": "uuid"},
            "minItems": 1,
            "maxItems": 100,
        }
        required += ["sequences"]
    elif trigger_type == "call_logged":
        properties["call_status"] = {"type": "string"}
        required += ["call_status"]
    elif trigger_type == "call_intelligence_ready":
        properties["intent"] = {
            "type": "string",
            "enum": ["any", "unknown", "low", "medium", "high"],
        }
        properties["min_ai_score"] = {"type": "integer", "minimum": 0, "maximum": 10}
        required += ["intent", "min_ai_score"]
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(set(required)),
        "additionalProperties": False,
    }


def _action_schema(action_type):
    schemas = {
        "start_sequence": {
            "sequence": {"type": "string", "format": "uuid"},
            "replace": {"type": "boolean"},
        },
        "move_stage": {
            "pipeline": {"type": "string", "format": "uuid"},
            "stage": {"type": "string", "format": "uuid"},
        },
        "message": {
            "body": {"type": "string", "maxLength": 20000},
            "account": {"type": "string", "format": "uuid"},
            "schedule": {"type": "string", "enum": ["relative", "fixed", "attribute"]},
            "duration": {"type": "integer", "minimum": 0, "maximum": 525600},
            "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
            "time": {"type": "string", "pattern": "^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"},
            "date_attribute": {"type": "string"},
        },
        "email": {
            "subject": {"type": "string", "maxLength": 255},
            "body": {"type": "string", "maxLength": 20000},
        },
        "reminder": {
            "duration": {"type": "integer", "minimum": 0, "maximum": 525600},
            "unit": {"type": "string", "enum": ["minutes", "hours", "days"]},
            "note": {"type": "string", "maxLength": 2000},
            "overwrite": {"type": "boolean"},
        },
        "attribute": {
            "key": {"type": "string"},
            "value": {"type": ["string", "number"]},
        },
        "stop_sequence": {},
        "ai": {"enabled": {"type": "boolean"}},
        "followup": {"enabled": {"type": "boolean"}},
    }
    properties = schemas[action_type]
    required_map = {
        "start_sequence": ["sequence", "replace"],
        "move_stage": ["pipeline", "stage"],
        "message": ["body", "account", "schedule"],
        "email": ["subject", "body"],
        "reminder": ["duration", "unit"],
        "attribute": ["key", "value"],
        "stop_sequence": [],
        "ai": ["enabled"],
        "followup": ["enabled"],
    }
    return {
        "type": "object",
        "properties": properties,
        "required": required_map[action_type],
        "additionalProperties": False,
    }


def list_workflow_triggers(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    catalog = _workflow_catalog_safe(organization)
    rows = [
        {"type": key, "label": value, "conditions_schema": _trigger_schema(key)}
        for key, value in catalog["triggers"].items()
    ]
    return ToolExecution(
        data={"triggers": rows},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"trigger_type_count": len(rows)},
    )


def list_workflow_actions(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    catalog = _workflow_catalog_safe(organization)
    rows = [
        {"type": key, "label": value, "action_schema": _action_schema(key)}
        for key, value in catalog["actions"].items()
    ]
    return ToolExecution(
        data={"actions": rows},
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={"action_type_count": len(rows)},
    )


def get_workflow_schema(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    catalog = _workflow_catalog_safe(organization)
    trigger_type = str((arguments or {}).get("trigger_type") or "").strip()
    action_type = str((arguments or {}).get("action_type") or "").strip()
    if trigger_type and trigger_type not in catalog["triggers"]:
        raise OperationsToolError("Unknown Workflow trigger_type.")
    if action_type and action_type not in catalog["actions"]:
        raise OperationsToolError("Unknown Workflow action_type.")

    trigger_schemas = (
        {trigger_type: _trigger_schema(trigger_type)}
        if trigger_type
        else {key: _trigger_schema(key) for key in catalog["triggers"]}
    )
    action_schemas = (
        {action_type: _action_schema(action_type)}
        if action_type
        else {key: _action_schema(key) for key in catalog["actions"]}
    )
    return ToolExecution(
        data={
            "trigger_schemas": trigger_schemas,
            "action_schemas": action_schemas,
            "catalog": {
                "sources": catalog["sources"],
                "pipelines": catalog["pipelines"],
                "stages": catalog["stages"],
                "sequences": catalog["sequences"],
                "attributes": catalog["attributes"],
                "accounts": catalog["accounts"],
                "call_statuses": catalog["call_statuses"],
                "call_intents": catalog["call_intents"],
                "timezone": catalog["timezone"],
            },
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "trigger_schema_count": len(trigger_schemas),
            "action_schema_count": len(action_schemas),
        },
    )


def validate_workflow_configuration(*, identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(
        identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ
    )
    data = (arguments or {}).get("data")
    _reject_secret_like_content(data, field="workflow")
    try:
        clean = validate_workflow_rule(organization, data)
    except ValidationError as exc:
        raise OperationsToolError("Workflow validation failed: " + "; ".join(exc.messages)) from exc
    _assert_workflow_safe_attribute_references(
        organization=organization,
        clean=clean,
    )
    return ToolExecution(
        data={
            "valid": True,
            "normalized": clean,
            "side_effects": False,
        },
        capability=CAP_ORGANIZATION_READ,
        target_type="organization",
        target_id=str(organization.id),
        audit_summary={
            "valid": True,
            "trigger_type": clean["trigger_type"],
            "action_type": clean["action_type"],
        },
    )
