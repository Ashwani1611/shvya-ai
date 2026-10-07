"""Canonical knowledge documents and guided-file controls for Operations MCP."""

import base64
import binascii
import hashlib
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.ai_engagement.models import Document, KnowledgeRepairRequest
from apps.ai_engagement.services.file_sharing import FileSharingError, FileSharingService
from apps.ai_engagement.services.knowledge_file_security import (
    KnowledgeFileSecurityError, validate_knowledge_file, validate_organization_knowledge_quota,
)
from apps.ai_engagement.services.knowledge_repair import KnowledgeRepairError, plan_repair, request_repair
from apps.ai_engagement.services.knowledge_source import KnowledgeSourceService, KnowledgeSourceServiceError
from apps.integrations.diagnostic_auth import sanitize_text
from apps.integrations.operations.constants import MAX_MCP_KNOWLEDGE_UPLOAD_BYTES
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_AI_CONFIG_WRITE, CAP_ORGANIZATION_READ, approval_required
from apps.integrations.operations_tools import (
    OperationsToolError, ToolExecution, _ensure_approved_proposal_unchanged, _organization_for,
    _proposal_digest, _reject_secret_like_content, _require_operations_capability, _write_gate,
)

_WRITE_FIELDS = {"reason", "dry_run", "approved", "approval_event_id"}
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"']+", re.I)


def _safe(value, limit=20000):
    def clean(match):
        try:
            parts = urlsplit(match.group())
            if parts.username or parts.password:
                return "[REDACTED_URL]"
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except ValueError:
            return "[REDACTED_URL]"
    return sanitize_text(_URL_RE.sub(clean, str(value or "")), limit=limit)


def _args(arguments, allowed):
    if not isinstance(arguments, dict) or set(arguments) - allowed:
        raise OperationsToolError("Unsupported knowledge document arguments.")


def _integer(value, field, minimum=1, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        raise OperationsToolError(f"{field} must be an integer between {minimum} and {maximum}.")
    return value


def _text(value, field, maximum, required=False):
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value or (required and not value.strip()):
        raise OperationsToolError(f"{field} must be valid {'nonempty ' if required else ''}text of at most {maximum} characters.")
    _reject_secret_like_content(value, field=field)
    return value.strip()


def _canonical(callback):
    try:
        return callback()
    except (KnowledgeSourceServiceError, KnowledgeFileSecurityError, FileSharingError, KnowledgeRepairError) as exc:
        raise OperationsToolError(_safe(str(exc), 1000)) from exc


def _rows(organization):
    return Document.objects.filter(organization=organization)


def _document(organization, document_id, lock=False):
    rows = _rows(organization)
    if lock:
        rows = rows.select_for_update()
    row = rows.filter(pk=_integer(document_id, "document_id")).first()
    if row is None:
        raise OperationsToolError("Knowledge document not found in this organization.")
    return row


def _read_scope(identity):
    organization = _organization_for(identity)
    _require_operations_capability(identity=identity, organization=organization, capability=CAP_ORGANIZATION_READ)
    return organization


def _status(document):
    counts = document.chunks.filter(organization_id=document.organization_id).aggregate(
        total=Count("pk"), active=Count("pk", filter=Q(is_active=True)),
        embedded=Count("pk", filter=Q(is_active=True, embedding__isnull=False)))
    mismatched = document.chunks.exclude(organization_id=document.organization_id).exists()
    return {"id": document.pk, "name": _safe(document.name, 255), "source_key": _safe(document.source_key, 500),
            "source_url": _safe(document.source_url, 2048), "version": document.version,
            "has_uploaded_file": bool(document.file), "processing_status": document.processing_status,
            "failure_help": document.failure_help if document.processing_status == "failed" else "",
            "is_active": document.is_active, "file_sharing_ready": document.file_sharing_ready,
            "share_instruction": _safe(document.share_instruction, 12000), "updated_at": document.updated_at.isoformat(),
            "chunk_count": counts["total"], "active_chunk_count": counts["active"], "embedded_chunk_count": counts["embedded"],
            "retrieval_ready": bool(not mismatched and document.is_active and document.processing_status == "completed" and counts["active"] and counts["active"] == counts["embedded"]),
            "chunk_scope_mismatch": mismatched}


def list_playbook_documents(*, identity, arguments):
    _args(arguments, {"limit", "cursor", "active_only", "guided_only", "processing_status"})
    organization = _read_scope(identity)
    limit = _integer(arguments.get("limit", 20), "limit", maximum=50)
    rows = _rows(organization)
    for field in ("active_only", "guided_only"):
        if type(arguments.get(field, False)) is not bool:
            raise OperationsToolError(f"{field} must be a boolean.")
    if arguments.get("active_only"):
        rows = rows.filter(is_active=True)
    if arguments.get("guided_only"):
        rows = rows.exclude(share_instruction="").exclude(file="")
    if "processing_status" in arguments:
        if arguments["processing_status"] not in Document.ProcessingStatus.values:
            raise OperationsToolError("Unsupported processing status.")
        rows = rows.filter(processing_status=arguments["processing_status"])
    if "cursor" in arguments:
        rows = rows.filter(pk__gt=_integer(arguments["cursor"], "cursor"))
    selected = list(rows.order_by("id")[:limit + 1])
    page = selected[:limit]
    return ToolExecution(data={"documents": [_status(row) for row in page], "count": len(page),
                               "next_cursor": page[-1].pk if len(selected) > limit else None},
                         capability=CAP_ORGANIZATION_READ, target_type="organization", target_id=str(organization.pk),
                         audit_summary={"operation": "list_playbook_documents", "count": len(page)})


def get_playbook_document(*, identity, arguments):
    _args(arguments, {"document_id", "include_chunks", "chunk_offset", "chunk_limit"})
    organization = _read_scope(identity)
    document = _document(organization, arguments.get("document_id"))
    include_chunks = arguments.get("include_chunks", True)
    if type(include_chunks) is not bool:
        raise OperationsToolError("include_chunks must be a boolean.")
    offset = _integer(arguments.get("chunk_offset", 0), "chunk_offset", 0, 100000)
    limit = _integer(arguments.get("chunk_limit", 10), "chunk_limit", maximum=20)
    latest_repair = KnowledgeRepairRequest.objects.filter(organization=organization, document=document).order_by("-created_at").first()
    repair_status = None
    if latest_repair:
        repair_status = {"request_id": str(latest_repair.pk), "operation": latest_repair.operation, "status": latest_repair.status,
                         "outcome_code": _safe(latest_repair.outcome_code, 100), "attempt": latest_repair.attempt,
                         "result_document_id": latest_repair.result_document_id, "updated_at": latest_repair.updated_at.isoformat()}
    result = {"document": _status(document), "latest_repair": repair_status, "chunks_included": include_chunks, "content_classification": "untrusted_knowledge_source"}
    if include_chunks:
        selected = list(document.chunks.filter(organization=organization).order_by("chunk_index", "pk").values(
            "id", "chunk_index", "content", "is_active")[offset:offset + limit + 1])
        page = selected[:limit]
        result.update({"chunks": [{"id": row["id"], "chunk_index": row["chunk_index"], "is_active": row["is_active"],
                                    "content": _safe(row["content"], 10000), "content_truncated": len(row["content"]) > 10000} for row in page],
                       "chunk_offset": offset, "next_chunk_offset": offset + limit if len(selected) > limit else None})
    return ToolExecution(data=result, capability=CAP_ORGANIZATION_READ, target_type="knowledge_document", target_id=str(document.pk),
                         audit_summary={"operation": "get_playbook_document", "chunks_included": include_chunks})


def _before(document):
    return {"id": document.pk, "name": document.name, "source_key": document.source_key, "version": document.version,
            "file": document.file.name, "share_instruction": document.share_instruction, "file_sharing_ready": document.file_sharing_ready,
            "processing_status": document.processing_status, "is_active": document.is_active, "updated_at": document.updated_at.isoformat()}


def _preview(identity, organization, operation, proposal, reason, data, document=None):
    return ToolExecution(data={"status": "DRY_RUN", **data,
                              "approval_required": approval_required(role=identity.role, organization=organization, capability=CAP_AI_CONFIG_WRITE)},
                         capability=CAP_AI_CONFIG_WRITE, target_type="knowledge_document" if document else "organization",
                         target_id=str(document.pk if document else organization.pk), reason=reason,
                         outcome=OperationsAuditEvent.Outcome.DRY_RUN,
                         audit_summary={"operation": operation, "proposal_digest": _proposal_digest(proposal)})


def _creation_state(organization, filename):
    state = _rows(organization).filter(source_key=filename).aggregate(version=Max("version"), last_update=Max("updated_at"), count=Count("pk"))
    return {"latest_version": state["version"] or 0, "version_count": state["count"],
            "latest_update": state["last_update"].isoformat() if state["last_update"] else None}


def _create(identity, arguments, operation, *, authored):
    _args(arguments, _WRITE_FIELDS | {"data"})
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_AI_CONFIG_WRITE,
                                  tool_name=operation, arguments=arguments)
    data = arguments.get("data")
    allowed = {"name", "filename", "share_instruction", "content", "ingest"} if authored else {"name", "filename", "share_instruction", "content_base64"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise OperationsToolError("Unsupported knowledge document data.")
    filename = _text(data.get("filename"), "filename", 255, True)
    if Path(filename).name != filename or "\\" in filename:
        raise OperationsToolError("filename must be a simple filename without a path.")
    name = _text(data.get("name", filename), "name", 255, True)
    instruction = _text(data.get("share_instruction", ""), "share_instruction", 12000)
    ingest = data.get("ingest", True)
    if type(ingest) is not bool:
        raise OperationsToolError("ingest must be a boolean.")
    if authored:
        if not filename.lower().endswith(".txt"):
            raise OperationsToolError("Authored Playbooks documents use a .txt filename.")
        content = _text(data.get("content"), "content", 100000, True)
        raw = content.encode("utf-8")
    else:
        encoded = data.get("content_base64")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_MCP_KNOWLEDGE_UPLOAD_BYTES + 2) // 3):
            raise OperationsToolError("content_base64 must encode a file of at most 512 KiB.")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise OperationsToolError("content_base64 is invalid.") from None
    if not raw or len(raw) > MAX_MCP_KNOWLEDGE_UPLOAD_BYTES:
        raise OperationsToolError("Knowledge MCP files must contain 1 byte to 512 KiB.")
    upload = ContentFile(raw, name=filename)
    _canonical(lambda: validate_knowledge_file(upload, filename=filename))
    _canonical(lambda: validate_organization_knowledge_quota(organization=organization, incoming_size=len(raw)))
    base = {"organization_id": str(organization.pk), "name": name, "filename": filename, "share_instruction": instruction,
            "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "ingest": ingest}
    proposal = {**base, "source_state": _creation_state(organization, filename)}
    if dry_run:
        return _preview(identity, organization, operation, proposal, reason,
                        {"name": name, "filename": filename, "size": len(raw), "share_instruction": instruction,
                         "guided_sharing_enabled_on_save": bool(instruction), "ingestion_will_be_queued": ingest,
                         "may_consume_ai_credits": ingest, "next_version": proposal["source_state"]["latest_version"] + 1})
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    from apps.ai_engagement.tasks import ingest_and_index_document
    document = None
    dispatch_state = {"status": "pending_commit" if ingest else "not_scheduled"}

    def dispatch_ingestion():
        try:
            ingest_and_index_document.delay(document_id=document.pk, organization_id=organization.pk)
            dispatch_state["status"] = "queued"
        except Exception:
            # The upload is already committed. Retain its bytes and make a
            # normal canonical failed-upload retry possible after broker repair.
            dispatch_state["status"] = "failed"
            Document.objects.filter(pk=document.pk, organization=organization, processing_status="pending").update(
                processing_status="failed", processing_error="Knowledge ingestion could not be queued; retry the upload.", updated_at=timezone.now())

    try:
        with transaction.atomic():
            locked_org = organization.__class__.objects.select_for_update().get(pk=organization.pk)
            _ensure_approved_proposal_unchanged(arguments=arguments, proposal={**base, "source_state": _creation_state(locked_org, filename)})
            source, document = _canonical(lambda: KnowledgeSourceService().create_file_source(
                organization=locked_org, uploaded_file=upload, name=name, share_instruction=instruction))
            document.refresh_from_db()
            if document.name != name or document.share_instruction != instruction or document.source_key != filename:
                raise OperationsToolError("Knowledge document post-write verification failed.")
            if ingest:
                transaction.on_commit(dispatch_ingestion)
    except Exception:
        if document is not None and document.file and not Document.objects.filter(pk=document.pk, organization=organization).exists():
            document.file.storage.delete(document.file.name)
        raise
    document.refresh_from_db()
    return ToolExecution(data={"status": "DISPATCH_FAILED" if dispatch_state["status"] == "failed" else "SAVED", "source_id": str(source.pk), "document": _status(document),
                               "ingestion_scheduled": ingest, "ingestion_queued": dispatch_state["status"] == "queued",
                               "dispatch_status": dispatch_state["status"], "verification": "document_saved", "processing_completed": False,
                               "readback_required": bool(ingest), "readback_tool": "get_playbook_document",
                               "guided_sharing_enabled_on_save": bool(instruction)},
                         capability=CAP_AI_CONFIG_WRITE, target_type="knowledge_document", target_id=str(document.pk), reason=reason,
                         audit_summary={"operation": operation, "ingestion_scheduled": ingest, "dispatch_status": dispatch_state["status"], "verification": "document_saved"})


def create_playbook_document(*, identity, arguments):
    return _create(identity, arguments, "create_playbook_document", authored=True)


def upload_knowledge_document_v2(*, identity, arguments):
    return _create(identity, arguments, "upload_knowledge_document", authored=False)


def update_knowledge_document(*, identity, arguments):
    _args(arguments, _WRITE_FIELDS | {"document_id", "changes"})
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_AI_CONFIG_WRITE,
                                  tool_name="update_knowledge_document", arguments=arguments)
    document = _document(organization, arguments.get("document_id"))
    changes = arguments.get("changes")
    if not isinstance(changes, dict) or not changes or set(changes) - {"name", "share_instruction"}:
        raise OperationsToolError("changes must contain name or share_instruction only.")
    changes = {key: _text(value, key, 255 if key == "name" else 12000, key == "name") for key, value in changes.items()}
    if "share_instruction" in changes and changes["share_instruction"]:
        if not document.file:
            raise OperationsToolError("AI-guided sharing requires an uploaded file.")
        try:
            with document.file.open("rb") as file:
                _canonical(lambda: validate_knowledge_file(file, filename=Path(document.file.name).name))
        except OSError:
            raise OperationsToolError("Stored file is unavailable; upload a valid replacement.") from None
    proposal = {"organization_id": str(organization.pk), "before": _before(document), "changes": changes}
    if dry_run:
        return _preview(identity, organization, "update_knowledge_document", proposal, reason,
                        {"document_id": document.pk, "proposed": changes, "guided_sharing_enabled_on_save": bool(changes.get("share_instruction", document.share_instruction)),
                         "messages_sent": False}, document)
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    with transaction.atomic():
        document = _document(organization, document.pk, lock=True)
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal={"organization_id": str(organization.pk), "before": _before(document), "changes": changes})
        for field, value in changes.items():
            setattr(document, field, value)
        fields = [*changes, "updated_at"]
        if "share_instruction" in changes:
            document.file_sharing_ready = False
            fields.append("file_sharing_ready")
        document.save(update_fields=fields)
        if changes.get("share_instruction"):
            _canonical(lambda: FileSharingService.prepare_uploaded_file(document=document))
        document.refresh_from_db()
        if any(getattr(document, field) != value for field, value in changes.items()):
            raise OperationsToolError("Knowledge metadata post-write verification failed.")
    return ToolExecution(data={"status": "FIXED", "document": _status(document), "messages_sent": False, "verification": "passed"},
                         capability=CAP_AI_CONFIG_WRITE, target_type="knowledge_document", target_id=str(document.pk), reason=reason,
                         audit_summary={"operation": "update_knowledge_document", "changed_fields": sorted(changes), "verification": "passed"})


def repair_knowledge_document(*, identity, arguments):
    _args(arguments, _WRITE_FIELDS | {"document_id", "operation"})
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(identity=identity, organization=organization, capability=CAP_AI_CONFIG_WRITE,
                                  tool_name="repair_knowledge_document", arguments=arguments)
    document = _document(organization, arguments.get("document_id"))
    plan = _canonical(lambda: plan_repair(organization=organization, document_id=document.pk))
    if arguments.get("operation") is not None and arguments["operation"] != plan["operation"]:
        raise OperationsToolError("The requested repair operation does not match this document's canonical repair plan.")
    proposal = {"organization_id": str(organization.pk), "repair": plan}
    if dry_run:
        return _preview(identity, organization, "repair_knowledge_document", proposal, reason,
                        {"plan": plan, "repair_available": plan["operation"] is not None, "processing_completed": False}, document)
    if not plan["operation"]:
        raise OperationsToolError("No safe repair is available: " + plan["reason"])
    _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    request = _canonical(lambda: request_repair(organization=organization, document_id=document.pk, expected_fingerprint=plan["fingerprint"]))
    request.refresh_from_db()
    status = {"succeeded": "COMPLETED", "dispatch_failed": "DISPATCH_FAILED", "failed": "FAILED", "skipped": "SKIPPED", "running": "RUNNING"}.get(request.status, "REQUESTED")
    return ToolExecution(data={"status": status,
                               "request_id": str(request.pk), "operation": request.operation, "repair_status": request.status,
                               "processing_completed": request.status == "succeeded", "may_consume_ai_credits": True,
                               "verification": "request_persisted", "readback_required": True, "readback_tool": "get_playbook_document"}, capability=CAP_AI_CONFIG_WRITE,
                         target_type="knowledge_document", target_id=str(document.pk), reason=reason,
                         audit_summary={"operation": "repair_knowledge_document", "request_id": str(request.pk), "repair_operation": request.operation})


AI_KNOWLEDGE_TOOL_HANDLERS = {name: globals()[name] for name in (
    "list_playbook_documents", "get_playbook_document", "create_playbook_document", "update_knowledge_document", "repair_knowledge_document",
)}
