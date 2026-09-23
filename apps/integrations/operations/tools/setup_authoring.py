"""Tenant-aware, non-mutating preparation for organization onboarding."""

from datetime import datetime, timedelta
import json
import re
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apps.channels.models import WhatsAppAccount
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.integrations.operations import setup_library
from apps.integrations.operations_policy import (
    CAP_SETUP_ARTIFACTS_PREPARE,
    CAP_SETUP_LIBRARY_READ,
)
from apps.integrations.operations_tools import (
    OperationsPermissionError,
    OperationsToolError,
    ToolExecution,
    _organization_for,
    _reject_secret_like_content,
    _require_operations_capability,
)
from apps.organizations.access import organization_is_active


def library_access(identity):
    """Static guidance may be read before a Superadmin selects a tenant."""
    organization = _organization_for(identity, required=False)
    if organization is not None and not organization_is_active(organization):
        raise OperationsPermissionError("The selected organization is inactive.")
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_SETUP_LIBRARY_READ)
    return organization


def _execution(data, organization, capability, summary):
    return ToolExecution(
        data=data,
        capability=capability,
        target_type="organization" if organization else "platform",
        target_id=str(organization.id) if organization else "",
        audit_summary=summary,
    )


def list_setup_library(identity, arguments):
    organization = library_access(identity)
    kind = arguments.get("kind", "all")
    entries = [item for item in setup_library.library_entries() if kind == "all" or item["kind"] == kind]
    cursor = arguments.get("cursor")
    start = 0
    if cursor:
        # A listed resource ID is a stable, transparent cursor, never a file path.
        indices = [i for i, entry in enumerate(entries) if entry["resource_id"] == cursor]
        if not indices:
            raise OperationsToolError("Setup library cursor does not match this filter.")
        start = indices[0] + 1
    end = start + arguments.get("limit", 30)
    selected = entries[start:end]
    data = {
        "entries": selected, "total": len(entries),
        "next_cursor": selected[-1]["resource_id"] if end < len(entries) and selected else None,
    }
    return _execution(data, organization, CAP_SETUP_LIBRARY_READ, {"resource_count": len(selected), "kind": kind})


def get_setup_library_resource(identity, arguments):
    organization = library_access(identity)
    try:
        data = setup_library.read_resource(
            setup_library.URI_PREFIX + arguments["resource_id"],
            offset=arguments.get("offset", 0), limit=arguments.get("limit", 12000),
        )
    except ValueError as exc:
        raise OperationsToolError(str(exc)) from exc
    return _execution(data, organization, CAP_SETUP_LIBRARY_READ, {
        "resource_id": arguments["resource_id"], "offset": data["_meta"]["offset"],
        "returned_chars": len(data["contents"][0]["text"]),
    })


def get_setup_variable_schema(identity, arguments):
    organization = library_access(identity)
    data = setup_library.variable_schema()
    return _execution(data, organization, CAP_SETUP_LIBRARY_READ, {"variable_count": len(data["variables"])})


def _validate_tenant_references(organization, variables):
    def reject():
        raise OperationsPermissionError("Setup variables reference an unavailable or cross-organization resource.")

    org_id = variables.get("SHVYA_ORGANIZATION_ID")
    if org_id is not None and org_id != str(organization.id):
        reject()
    pipeline_id = variables.get("SHVYA_PIPELINE_ID")
    for name, value in variables.items():
        if value is None:
            continue
        if name.endswith("_ID"):
            try:
                UUID(value)
            except (TypeError, ValueError, AttributeError):
                reject()
        if name == "SHVYA_PIPELINE_ID":
            if not Pipeline.objects.filter(id=value, organization=organization, is_active=True).exists():
                reject()
        elif name.endswith("_STAGE_ID"):
            stages = Stage.objects.filter(id=value, pipeline__organization=organization, pipeline__is_active=True, is_active=True)
            if pipeline_id:
                stages = stages.filter(pipeline_id=pipeline_id)
            if not stages.exists():
                reject()
        elif name == "SHVYA_WHATSAPP_ACCOUNT_ID":
            if not WhatsAppAccount.objects.filter(id=value, organization=organization, is_active=True).exists():
                reject()
        elif name.endswith("_ATTRIBUTE_KEY"):
            if not AttributeDefinition.objects.filter(key=value, organization=organization, is_active=True).exists():
                reject()


def render_setup_template(identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_SETUP_ARTIFACTS_PREPARE)
    try:
        data = setup_library.render_template(arguments["template_id"], arguments["variables"])
    except ValueError as exc:
        raise OperationsToolError(str(exc)) from exc
    # Validate registered shapes first so untrusted keys never enter error text.
    _reject_secret_like_content(arguments["variables"], field="setup_variables")
    _validate_tenant_references(organization, arguments["variables"])
    data["organization_id"] = str(organization.id)
    data["validation"]["tenant_references_checked"] = True
    return _execution(data, organization, CAP_SETUP_ARTIFACTS_PREPARE, {
        "template_id": arguments["template_id"], "draft_only": True,
        "output_chars": len(data["text"]), "variable_count": len(arguments["variables"]),
    })


def _export_text(value, *, limit=200, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise OperationsToolError("Group export text is missing, invalid or exceeds its field limit.")
    _reject_secret_like_content(value, field="group_export")
    if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
        raise OperationsToolError("Group export contains unsupported control characters.")
    if re.search(r"data:[^\s,]+[;,]", value, re.I):
        raise OperationsToolError("Group exports accept media references, not embedded media.")
    for match in re.finditer(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"']+", value, re.I):
        try:
            url = urlsplit(match.group())
        except ValueError as exc:
            raise OperationsToolError("Group export URL references are invalid.") from exc
        if url.username or url.password or url.query:
            raise OperationsToolError("Group export URLs must omit credentials and query parameters.")
    return value


def _inline_source(value):
    """Keep untrusted metadata on one line inside the evidence block."""
    return json.dumps(value, ensure_ascii=False)[1:-1]


def prepare_group_export(data, *, organization_id, chat_id, timezone, since=None, until=None, limit=100):
    """Normalize only the supplied one-group export; never contact a provider."""
    fields = {"schema_version", "organization_id", "chat_id", "chat_name", "source_id", "coverage_note", "messages"}
    if not isinstance(data, dict) or set(data) != fields or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise OperationsToolError("Group export must use the documented version 1 fields.")
    if data["organization_id"] != organization_id or data["chat_id"] != chat_id:
        raise OperationsPermissionError("Group export organization or chat does not match the active context.")
    for name in fields - {"schema_version", "messages"}:
        _export_text(data[name], limit=2000 if name == "coverage_note" else 200)
    if type(limit) is not int or not 1 <= limit <= 500:
        raise OperationsToolError("Group export limit must be between 1 and 500.")
    try:
        for value in (since, until):
            if value is not None and (not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None):
                raise ValueError
        zone = ZoneInfo(timezone)
        lower = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=zone) if since else None
        upper = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=zone) + timedelta(days=1) if until else None
        if lower and upper and lower >= upper:
            raise ValueError
    except (ValueError, TypeError, ZoneInfoNotFoundError, OverflowError) as exc:
        raise OperationsToolError("Supply a valid timezone and ordered date range.") from exc
    rows = data["messages"]
    if not isinstance(rows, list) or len(rows) > 5000:
        raise OperationsToolError("Group export must contain at most 5000 messages.")
    if len(json.dumps(data, ensure_ascii=False)) > 800000:
        raise OperationsToolError("Group export exceeds the supported size.")
    seen, normalized = {}, []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"id", "timestamp", "sender", "sender_role", "text", "media"}:
            raise OperationsToolError("Group export message fields do not match the documented format.")
        for name in ("id", "timestamp", "sender", "sender_role", "text"):
            _export_text(row[name], limit=4000 if name == "text" else 200, empty=name == "text")
        if row["sender_role"] not in {"client", "shvya", "other", "unknown"}:
            raise OperationsToolError("Group export contains an unknown sender role.")
        try:
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
            stamp.astimezone(zone)
        except (ValueError, OverflowError) as exc:
            raise OperationsToolError("Group export timestamps require valid explicit timezone offsets.") from exc
        if not isinstance(row["media"], list) or len(row["media"]) > 10:
            raise OperationsToolError("Each message supports at most ten media references.")
        for media in row["media"]:
            if not isinstance(media, dict) or not set(media).issubset({"type", "filename"}) or "type" not in media:
                raise OperationsToolError("Media supports type and optional filename only; URLs are not fetched.")
            for value in media.values():
                _export_text(value)
        if row["id"] in seen:
            if seen[row["id"]] != row:
                raise OperationsToolError("Group export contains conflicting duplicate message IDs.")
            continue
        seen[row["id"]] = row
        normalized.append((stamp, row))
    normalized.sort(key=lambda pair: (pair[0], pair[1]["id"]))
    within = [(stamp, row) for stamp, row in normalized if (not lower or stamp >= lower) and (not upper or stamp < upper)]
    chosen, remaining = [], 60000
    # Take newest complete messages; never silently cut a customer's text.
    for stamp, row in reversed(within[-limit:]):
        block = f"## {stamp.astimezone(zone).isoformat()}\n> Sender: {_inline_source(row['sender'])} ({row['sender_role']})\n> Source message: {_inline_source(row['id'])}\n"
        block += "\n".join("> " + line for line in row["text"].splitlines()) + "\n"
        for media in row["media"]:
            block += f"> [Uninspected {_inline_source(media['type'])}: {_inline_source(media.get('filename', 'filename not supplied'))}]\n"
        if len(block) > remaining:
            break
        chosen.append(block)
        remaining -= len(block)
    return {
        "organization_id": organization_id, "chat_id": chat_id,
        "chat_name": data["chat_name"], "source_id": data["source_id"],
        "coverage_note": data["coverage_note"], "timezone": timezone,
        "unique_source_messages": len(normalized), "matching_messages": len(within),
        "selected_messages": len(chosen), "duplicates_removed": len(rows) - len(normalized),
        "truncated": len(chosen) < len(within), "live_retrieval": False,
        "untrusted_source_material": True, "media_inspected": False,
        "formatted_text": "\n".join(reversed(chosen)),
    }


def analyze_setup_group_export(identity, arguments):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_SETUP_ARTIFACTS_PREPARE)
    data = prepare_group_export(organization_id=str(organization.id), **arguments)
    return _execution(data, organization, CAP_SETUP_ARTIFACTS_PREPARE, {
        key: data[key] for key in ("unique_source_messages", "matching_messages", "selected_messages", "truncated")
    })
