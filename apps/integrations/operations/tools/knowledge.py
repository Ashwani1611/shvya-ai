"""Knowledge source/document tools for SHVYA Operations MCP."""

from __future__ import annotations

import base64
import binascii
import re

from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import Count, Q

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.services.knowledge import (
    KnowledgeExtractionError,
    KnowledgeIngestionService,
)
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.integrations.operations.constants import MAX_MCP_KNOWLEDGE_UPLOAD_BYTES
from apps.integrations.operations_models import OperationsAuditEvent
from apps.integrations.operations_policy import CAP_AI_CONFIG_WRITE, approval_required
from apps.integrations.operations_tools import (
    OperationsApprovalRequired,
    OperationsToolError,
    ToolExecution,
    _ensure_approved_proposal_unchanged,
    _organization_for,
    _proposal_digest,
    _reject_secret_like_content,
    _write_gate,
)

def _knowledge_source(organization, source_id):
    source = KnowledgeSource.objects.filter(
        pk=source_id,
        organization=organization,
    ).first()
    if source is None:
        raise OperationsToolError("Knowledge source not found in this organization.")
    return source


def create_knowledge_source(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="create_knowledge_source",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a knowledge source object.")
    _reject_secret_like_content(data, field="knowledge_source")
    source_type = str(data.get("source_type") or "url").strip()
    if source_type != KnowledgeSource.SourceType.URL:
        raise OperationsToolError(
            "create_knowledge_source supports URL sources; use upload_knowledge_document for files."
        )
    name = str(data.get("name") or "").strip()
    url = str(data.get("url") or "").strip()
    ingest = data.get("ingest", True)
    if not isinstance(ingest, bool):
        raise OperationsToolError("ingest must be a boolean.")
    try:
        normalized = KnowledgeIngestionService().normalize_url(url)
    except KnowledgeExtractionError as exc:
        raise OperationsToolError(str(exc)) from exc
    duplicate = KnowledgeSource.objects.filter(
        organization=organization,
        source_type=KnowledgeSource.SourceType.URL,
        url=normalized,
        is_active=True,
    ).first()
    if duplicate:
        raise OperationsToolError("An active knowledge URL source already exists.")
    proposal = {
        "organization_id": str(organization.id),
        "source_type": "url",
        "name": name,
        "url": normalized,
        "ingest": ingest,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "source_type": "url",
                "url_host": re.sub(r"^https?://", "", normalized).split("/", 1)[0],
                "ingest": ingest,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "create_knowledge_source", "proposal_digest": _proposal_digest(proposal)},
        )

    from apps.ai_engagement.tasks import ingest_and_index_url_source

    with transaction.atomic():
        locked_org = organization.__class__.objects.select_for_update().get(pk=organization.pk)
        if KnowledgeSource.objects.filter(
            organization=locked_org,
            source_type=KnowledgeSource.SourceType.URL,
            url=normalized,
            is_active=True,
        ).exists():
            raise OperationsApprovalRequired(
                "Knowledge sources changed after review. Run a fresh dry-run."
            )
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        try:
            source = KnowledgeSourceService().create_url_source(
                organization=locked_org,
                url=normalized,
                name=name,
            )
        except KnowledgeSourceServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
        if ingest:
            transaction.on_commit(
                lambda source_id=source.id, org_id=locked_org.id: ingest_and_index_url_source.delay(
                    source_id, org_id
                )
            )
    return ToolExecution(
        data={
            "status": "FIXED",
            "source": {
                "id": str(source.id),
                "source_type": source.source_type,
                "name": source.name,
                "active": source.is_active,
            },
            "ingestion_queued": ingest,
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_source",
        target_id=str(source.id),
        reason=reason,
        audit_summary={"operation": "create_knowledge_source", "ingestion_queued": ingest, "verification": "passed"},
    )


def _decode_upload(data):
    filename = str(data.get("filename") or "").strip()
    encoded = str(data.get("content_base64") or "").strip()
    if not filename or not encoded:
        raise OperationsToolError("filename and content_base64 are required.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise OperationsToolError("content_base64 is not valid base64.") from exc
    if not raw or len(raw) > MAX_MCP_KNOWLEDGE_UPLOAD_BYTES:
        raise OperationsToolError("Knowledge upload through MCP must be between 1 byte and 512 KiB.")
    return filename, raw


def upload_knowledge_document(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="upload_knowledge_document",
        arguments=arguments,
    )
    data = (arguments or {}).get("data")
    if not isinstance(data, dict):
        raise OperationsToolError("data must be a knowledge document object.")
    filename, raw = _decode_upload(data)
    name = str(data.get("name") or filename).strip()
    proposal = {
        "organization_id": str(organization.id),
        "filename": filename,
        "name": name,
        "size": len(raw),
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "filename": filename,
                "size": len(raw),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "ingestion_will_be_queued": True,
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="organization",
            target_id=str(organization.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "upload_knowledge_document", "proposal_digest": _proposal_digest(proposal), "size": len(raw)},
        )

    from apps.ai_engagement.tasks import ingest_and_index_document

    with transaction.atomic():
        locked_org = organization.__class__.objects.select_for_update().get(pk=organization.pk)
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
        upload = ContentFile(raw, name=filename)
        try:
            source, document = KnowledgeSourceService().create_file_source(
                organization=locked_org,
                uploaded_file=upload,
                name=name,
            )
        except KnowledgeSourceServiceError as exc:
            raise OperationsToolError(str(exc)) from exc
        transaction.on_commit(
            lambda document_id=document.id, org_id=locked_org.id: ingest_and_index_document.delay(
                document_id, org_id
            )
        )
    return ToolExecution(
        data={
            "status": "FIXED",
            "source_id": str(source.id),
            "document": {
                "id": str(document.id),
                "name": document.name,
                "processing_status": document.processing_status,
                "active": document.is_active,
            },
            "ingestion_queued": True,
            "verification": "passed",
        },
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_document",
        target_id=str(document.id),
        reason=reason,
        audit_summary={"operation": "upload_knowledge_document", "source_id": str(source.id), "verification": "passed"},
    )


def publish_knowledge_document(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="publish_knowledge_document",
        arguments=arguments,
    )
    document = Document.objects.filter(
        pk=(arguments or {}).get("document_id"),
        organization=organization,
    ).annotate(
        active_chunk_count=Count(
            "chunks",
            filter=Q(chunks__is_active=True),
        ),
        embedded_chunk_count=Count(
            "chunks",
            filter=Q(chunks__embedding__isnull=False, chunks__is_active=True),
        ),
    ).first()
    if document is None:
        raise OperationsToolError("Knowledge document not found in this organization.")
    if document.processing_status != Document.ProcessingStatus.COMPLETED:
        raise OperationsToolError("Only completed knowledge documents can be published.")
    if not document.active_chunk_count or document.embedded_chunk_count != document.active_chunk_count:
        raise OperationsToolError("Knowledge document must be fully embedded before publication.")
    proposal = {
        "document_id": str(document.id),
        "source_key": document.source_key,
        "version": document.version,
        "before_active": document.is_active,
        "after_active": True,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "document_id": str(document.id),
                "version": document.version,
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="knowledge_document",
            target_id=str(document.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "publish_knowledge_document", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(
            pk=document.pk, organization=organization
        ).first()
        if locked is None:
            raise OperationsApprovalRequired("Knowledge document changed after review. Run a fresh dry-run.")
        locked_proposal = {
            "document_id": str(locked.id),
            "source_key": locked.source_key,
            "version": locked.version,
            "before_active": locked.is_active,
            "after_active": True,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        try:
            saved = KnowledgeIngestionService().publish_document_version(locked)
        except KnowledgeExtractionError as exc:
            raise OperationsToolError(str(exc)) from exc
    return ToolExecution(
        data={"status": "FIXED", "document_id": str(saved.id), "active": saved.is_active, "verification": "passed"},
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_document",
        target_id=str(saved.id),
        reason=reason,
        audit_summary={"operation": "publish_knowledge_document", "verification": "passed"},
    )


def archive_knowledge_document(*, identity, arguments):
    organization = _organization_for(identity)
    dry_run, reason = _write_gate(
        identity=identity,
        organization=organization,
        capability=CAP_AI_CONFIG_WRITE,
        tool_name="archive_knowledge_document",
        arguments=arguments,
    )
    document = Document.objects.filter(
        pk=(arguments or {}).get("document_id"),
        organization=organization,
    ).first()
    if document is None:
        raise OperationsToolError("Knowledge document not found in this organization.")
    proposal = {
        "document_id": str(document.id),
        "before_active": document.is_active,
        "after_active": False,
    }
    if not dry_run:
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=proposal)
    if dry_run:
        return ToolExecution(
            data={
                "status": "DRY_RUN",
                "document_id": str(document.id),
                "approval_required": approval_required(
                    role=identity.role,
                    organization=organization,
                    capability=CAP_AI_CONFIG_WRITE,
                ),
                "affected_chunk_count": document.chunks.filter(is_active=True).count(),
                "reversible": True,
            },
            capability=CAP_AI_CONFIG_WRITE,
            target_type="knowledge_document",
            target_id=str(document.id),
            reason=reason,
            outcome=OperationsAuditEvent.Outcome.DRY_RUN,
            audit_summary={"operation": "archive_knowledge_document", "proposal_digest": _proposal_digest(proposal)},
        )
    with transaction.atomic():
        locked = Document.objects.select_for_update().filter(
            pk=document.pk, organization=organization
        ).first()
        if locked is None:
            raise OperationsApprovalRequired("Knowledge document changed after review. Run a fresh dry-run.")
        locked_proposal = {
            "document_id": str(locked.id),
            "before_active": locked.is_active,
            "after_active": False,
        }
        _ensure_approved_proposal_unchanged(arguments=arguments, proposal=locked_proposal)
        # Retrieval already requires document.is_active=True. Keep chunk
        # activation intact so archive is genuinely reversible by republishing
        # this completed, embedded version later.
        locked.is_active = False
        locked.save(update_fields=["is_active", "updated_at"])
    return ToolExecution(
        data={"status": "FIXED", "document_id": str(document.id), "active": False, "verification": "passed"},
        capability=CAP_AI_CONFIG_WRITE,
        target_type="knowledge_document",
        target_id=str(document.id),
        reason=reason,
        audit_summary={"operation": "archive_knowledge_document", "verification": "passed"},
    )
