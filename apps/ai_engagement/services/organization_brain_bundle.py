"""Generate one tenant's AI Brain configuration from canonical database records.

The bundle is a versioned configuration snapshot, not a second source of truth
or a prompt containing the entire knowledge index. No binaries, chunk content,
vectors, credentials or customer state are loaded. Runtime callers project this
same snapshot into their existing bounded context.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from django.utils import timezone

from apps.ai_engagement.services.confidentiality import is_sensitive_attribute_definition
from apps.ai_engagement.services.playbook import parse_playbook
from apps.ai_engagement.services.tenant_guard import OBJECT_NOT_IN_ORGANIZATION, TenantGuard, TenantScopeError


BRAIN_BUNDLE_SCHEMA_VERSION = 1
BRAIN_BUNDLE_KIND = "shvya.organization_ai_brain"

_AI_FIELDS = (
    "about", "bot_languages", "ai_playbook", "qualification_model",
    "sales_support_model", "summary_model", "ai_enabled", "bump_up_enabled",
    "bump_up_count", "created_at", "updated_at",
)
_ATTRIBUTE_FIELDS = (
    "id", "name", "key", "field_type", "description", "options",
    "display_order", "is_active", "created_at", "updated_at",
)
_PIPELINE_FIELDS = (
    "id", "name", "description", "country_code", "phone_number",
    "ai_enabled", "is_active", "created_at", "updated_at",
)
_STAGE_FIELDS = (
    "id", "pipeline_id", "name", "description", "display_order", "color",
    "ai_on", "is_active", "config", "created_at", "updated_at",
)
_SOURCE_FIELDS = (
    "id", "source_type", "name", "url", "is_active", "created_at", "updated_at",
)
_DOCUMENT_FIELDS = (
    "id", "name", "version", "source_url", "file", "processing_status",
    "is_active", "file_sharing_ready", "share_instruction", "created_at", "updated_at",
)


def _timestamp(value):
    return value.isoformat() if value is not None else None


def _record(row, fields):
    """Copy explicitly selected fields, preserving text without truncation."""
    output = {field: row.get(field) for field in fields}
    for key in ("id", "pipeline_id"):
        if key in output:
            output[key] = str(output[key])
    for key in ("created_at", "updated_at"):
        if key in output:
            output[key] = _timestamp(output[key])
    return output


def _safe_source_url(value):
    """Export source identity without URL authorization material."""
    try:
        parsed = urlsplit(str(value or "").strip())
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not hostname:
        return ""
    # parsed.netloc includes user:password; reconstruct only host and port.
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _stage_config(value, allowed_attribute_ids):
    """Only the stage configuration currently supported by the CRM is portable."""
    if not isinstance(value, dict) or not isinstance(value.get("required_attribute_ids"), list):
        return {}
    selected = []
    for raw_id in value["required_attribute_ids"]:
        try:
            attribute_id = str(UUID(str(raw_id)))
        except (TypeError, ValueError, AttributeError):
            continue
        if attribute_id in allowed_attribute_ids and attribute_id not in selected:
            selected.append(attribute_id)
    return {"required_attribute_ids": selected}


def _assemble_bundle(*, organization, ai, faqs, attributes, pipelines, stages, sources, documents):
    """Pure serialization boundary shared by the database builder and tests."""
    organization = {"id": str(organization["id"]), "name": organization["name"],
                    "timezone": organization["timezone"]}
    ai_data = _record(ai, _AI_FIELDS) if ai else {
        "about": "", "bot_languages": "", "ai_playbook": "", "qualification_model": "",
        "sales_support_model": "", "summary_model": "", "ai_enabled": False,
        "bump_up_enabled": False, "bump_up_count": 0, "created_at": None, "updated_at": None,
    }
    sections = parse_playbook(ai_data["ai_playbook"] or "")
    attribute_data = [_record(row, _ATTRIBUTE_FIELDS) for row in attributes]
    # Attribute options have a typed public schema, not arbitrary JSON settings.
    for item in attribute_data:
        options = item["options"]
        item["options"] = [option for option in options if isinstance(option, str)] if isinstance(options, list) else []
    allowed_attribute_ids = {
        item["id"] for item in attribute_data
        if item["is_active"] and not is_sensitive_attribute_definition(item)
    }
    stages_by_pipeline = {}
    pipeline_ids = {str(row["id"]) for row in pipelines}
    for row in stages:
        if str(row["pipeline_id"]) not in pipeline_ids:
            raise TenantScopeError(object_type="stage")
        stage = _record(row, _STAGE_FIELDS)
        stage["config"] = _stage_config(row.get("config"), allowed_attribute_ids)
        stages_by_pipeline.setdefault(stage.pop("pipeline_id"), []).append(stage)
    pipeline_data = []
    for row in pipelines:
        pipeline = _record(row, _PIPELINE_FIELDS)
        pipeline["stages"] = stages_by_pipeline.get(pipeline["id"], [])
        pipeline_data.append(pipeline)
    source_data = [_record(row, _SOURCE_FIELDS) for row in sources]
    for item in source_data:
        item["url"] = _safe_source_url(item["url"])
    document_data = []
    for row in documents:
        item = _record(row, _DOCUMENT_FIELDS)
        stored_file = str(item.pop("file") or "")
        filename = PurePosixPath(stored_file.replace("\\", "/")).name if stored_file else ""
        item["source_url"] = _safe_source_url(item["source_url"])
        item["has_file"] = bool(stored_file)
        item["file_reference"] = {
            "document_id": item["id"], "filename": filename,
            "file_extension": PurePosixPath(filename).suffix.lower(),
        } if stored_file else None
        document_data.append(item)
    payload = {
        "kind": BRAIN_BUNDLE_KIND, "schema_version": BRAIN_BUNDLE_SCHEMA_VERSION,
        "organization": organization, "ai": ai_data,
        "qualification": {"questions": sections["qualification_questions"],
                          "criteria": sections["qualification_criteria"]},
        "faqs": [_record(row, ("id", "question", "answer", "is_active", "created_at", "updated_at")) for row in faqs],
        "crm": {"attributes": attribute_data, "pipelines": pipeline_data},
        "knowledge": {"sources": source_data, "documents": document_data},
        "scope": {
            "record_states": "active_and_inactive",
            "knowledge": "source_and_document_version_metadata",
            "stage_configuration": "same_organization_active_nonsensitive_required_attribute_ids",
            "attribute_options": "string_options_only",
            "excluded": ["credentials", "arbitrary_settings", "raw_storage_keys", "document_binaries",
                         "knowledge_chunk_content", "embedding_vectors", "customer_state"],
            "source_urls": "host_port_path_without_userinfo_query_or_fragment",
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {**payload, "revision": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
            "generated_at": timezone.now().isoformat()}


def get_organization_ai_brain_bundle(*, organization):
    """Read a fresh, complete configuration snapshot in eight scoped queries.

    Do not substitute timestamp/count-only caching: canonical readiness and
    publication writers also use bulk updates without advancing updated_at.
    Exact exported rows determine the revision; no knowledge chunks are scanned.
    """
    guard = TenantGuard(organization)
    from apps.ai_engagement.models import Document, FAQ, KnowledgeSource, OrgInfo
    from apps.crm.models import AttributeDefinition, Pipeline, Stage
    from apps.organizations.models import Organization

    org_row = Organization.objects.filter(pk=guard.organization_id).values("id", "name", "timezone").first()
    if org_row is None:
        raise TenantScopeError(OBJECT_NOT_IN_ORGANIZATION, object_type="organization")
    ai = OrgInfo.objects.filter(organization_id=guard.organization_id).values(*_AI_FIELDS).first() or {}
    faqs = list(FAQ.objects.filter(organization_id=guard.organization_id).order_by("pk").values(
        "id", "question", "answer", "is_active", "created_at", "updated_at"))
    attributes = list(AttributeDefinition.objects.filter(organization_id=guard.organization_id)
                      .order_by("display_order", "pk").values(*_ATTRIBUTE_FIELDS))
    pipelines = list(Pipeline.objects.filter(organization_id=guard.organization_id)
                     .order_by("name", "pk").values(*_PIPELINE_FIELDS))
    stages = list(Stage.objects.filter(pipeline__organization_id=guard.organization_id)
                  .order_by("pipeline_id", "display_order", "pk").values(*_STAGE_FIELDS))
    sources = list(KnowledgeSource.objects.filter(organization_id=guard.organization_id)
                   .order_by("pk").values(*_SOURCE_FIELDS))
    documents = list(Document.objects.filter(organization_id=guard.organization_id)
                     .order_by("pk").values(*_DOCUMENT_FIELDS))
    return _assemble_bundle(organization=org_row, ai=ai, faqs=faqs, attributes=attributes,
                            pipelines=pipelines, stages=stages, sources=sources, documents=documents)
