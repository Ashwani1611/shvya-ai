"""Bounded, tenant-scoped repair plans over the existing ingestion services.

Default inspection is read-only. Execution requires the exact reviewed plan
fingerprint and explicit credit consent. No customer turn automatically reindexes.
"""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

from django.db import transaction


ACTIVE_STATES = ("queued", "running", "retrying", "dispatch_failed", "uncertain")
MAX_CHUNKS = 2000
TASKS = {"reindex_missing": "reindex_document_embeddings", "retry_upload": "ingest_and_index_document",
         "retry_url": "ingest_and_index_url_source"}


class SourceRepairError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def inspect_document(*, organization, document_id):
    from apps.ai_engagement.models import AIKnowledgeRepair, Document, KnowledgeSource

    document = Document.objects.filter(pk=document_id, organization=organization).first()
    if document is None:
        raise SourceRepairError("document_not_in_organization")
    chunks = list(document.chunks.order_by("pk").values(
        "id", "organization_id", "is_active", "content", "embedding",
    )[:MAX_CHUNKS + 1])
    # Hash source text/vector presence without returning either in diagnostics.
    chunk_marks = [(row["id"], row["organization_id"], row["is_active"],
                    _hash(row["content"]), _hash(row["embedding"].tolist() if hasattr(row["embedding"], "tolist") else row["embedding"]))
                   for row in chunks]
    active = [row for row in chunks if row["is_active"]]
    source = None
    if document.source_url:
        sources = list(KnowledgeSource.objects.filter(organization=organization, source_type="url",
                                                      url=document.source_key).order_by("pk")[:2])
        source = sources[0] if len(sources) == 1 else None
    source_mark = (source.pk, source.url, source.is_active, source.updated_at) if source else None
    document_fp = _hash({"document": document.pk, "version": document.version,
        "source": document.source_key, "url": document.source_url, "file": document.file.name,
        "active": document.is_active, "status": document.processing_status,
        "updated": document.updated_at, "chunks": chunk_marks, "source_state": source_mark})
    last = AIKnowledgeRepair.objects.filter(organization=organization, document=document).order_by("-created_at", "-pk").first()
    prior = (str(last.pk), last.state, last.updated_at) if last else None
    plan_fp = _hash({"document": document_fp, "prior": prior})
    action, reason = None, "not_repairable"
    if not organization.is_active:
        reason = "organization_inactive"
    elif len(chunks) > MAX_CHUNKS:
        reason = "chunk_limit_requires_manual_review"
    elif any(row["organization_id"] != organization.pk for row in chunks):
        reason = "chunk_tenant_mismatch"
    elif not document.source_key:
        reason = "source_identity_missing"
    elif Document.objects.filter(organization=organization, source_key=document.source_key,
                                  version__gt=document.version).exists():
        reason = "superseded_version"
    elif last and last.state in ACTIVE_STATES:
        reason = "repair_already_in_progress"
    elif document.source_url and (source is None or not source.is_active):
        reason = "url_source_inactive_or_ambiguous"
    elif document.processing_status in {"pending", "processing"}:
        reason = "ingestion_already_in_progress"
    elif document.processing_status == "failed":
        if active:
            action, reason = "reindex_missing", "recover_extracted_content"
        elif document.file:
            action, reason = "retry_upload", "retry_original_upload"
        elif source:
            action, reason = "retry_url", "refetch_authorized_url_as_new_version"
        else:
            reason = "source_bytes_unavailable"
    elif document.processing_status == "completed":
        if active and any(row["embedding"] is None for row in active):
            action, reason = "reindex_missing", "fill_missing_embeddings"
        else:
            reason = "healthy" if active else "reupload_required_no_extracted_content"
    return {"document_id": document.pk, "version": document.version, "action": action,
            "reason": reason, "repairable": bool(action), "fingerprint": plan_fp,
            "document_fingerprint": document_fp, "source_id": source.pk if source else None,
            "chunk_count": len(active), "missing_embeddings": sum(row["embedding"] is None for row in active),
            "is_active": document.is_active, "credit_usage_possible": bool(action),
            "last_request_id": str(last.pk) if last else None}


def request_repair(*, organization, document_id, expected_fingerprint, allow_credits=False):
    from apps.ai_engagement.models import AIKnowledgeRepair, Document
    from apps.organizations.models import Organization

    if allow_credits is not True:
        raise SourceRepairError("explicit_credit_consent_required")
    with transaction.atomic():
        # Same publication lock order as the canonical ingestion service.
        locked_org = Organization.objects.select_for_update().get(pk=organization.pk)
        document = Document.objects.select_for_update().filter(pk=document_id, organization=locked_org).first()
        if document is None:
            raise SourceRepairError("document_not_in_organization")
        duplicate = AIKnowledgeRepair.objects.filter(organization=locked_org, document=document,
                                                     fingerprint=expected_fingerprint).order_by("-created_at").first()
        if duplicate is not None:
            return duplicate
        plan = inspect_document(organization=locked_org, document_id=document.pk)
        if plan["fingerprint"] != expected_fingerprint:
            raise SourceRepairError("repair_plan_changed_review_again")
        if not plan["repairable"]:
            raise SourceRepairError(plan["reason"])
        request = AIKnowledgeRepair.objects.create(organization=locked_org, document=document,
            source_id=plan["source_id"], source_version=plan["version"], action=plan["action"],
            fingerprint=plan["fingerprint"], document_fingerprint=plan["document_fingerprint"])
        transaction.on_commit(lambda: dispatch_repair(organization_id=locked_org.pk, request_id=request.pk))
    return request


def dispatch_repair(*, organization_id, request_id):
    from apps.ai_engagement.models import AIKnowledgeRepair
    from apps.ai_engagement import tasks
    from django.utils import timezone

    request = AIKnowledgeRepair.objects.filter(pk=request_id, organization_id=organization_id, state__in=["queued", "dispatch_failed"]).first()
    if request is None:
        return False
    kwargs = {"organization_id": str(organization_id), "repair_request_id": str(request.pk)}
    if request.action == "retry_url":
        kwargs["source_id"] = request.source_id
    else:
        kwargs["document_id"] = request.document_id
    if request.action == "reindex_missing":
        kwargs["only_missing"] = True
    try:
        getattr(tasks, TASKS[request.action]).apply_async(kwargs=kwargs, task_id=str(request.pk))
    except Exception:
        # Broker acceptance can be uncertain. Retrying this SAME ticket is safe:
        # the worker's durable claim allows only one execution attempt number.
        AIKnowledgeRepair.objects.filter(pk=request.pk, state="queued").update(
            state="dispatch_failed", outcome_code="dispatch_not_confirmed", updated_at=timezone.now())
        return False
    return True


def repair_report(*, organization, request_id):
    from apps.ai_engagement.models import AIKnowledgeRepair, Document
    from apps.ai_engagement.services.usage_attribution import UsageCapture, usage_report

    request = AIKnowledgeRepair.objects.filter(pk=UUID(str(request_id)), organization=organization).first()
    if request is None:
        raise SourceRepairError("repair_not_in_organization")
    doc = Document.objects.filter(pk=request.result_document_id or request.document_id, organization=organization).first()
    return {"request_id": str(request.pk), "document_id": request.document_id, "action": request.action,
            "state": request.state, "outcome": request.outcome_code, "last_attempt": request.last_attempt,
            "result_document_id": request.result_document_id, "published": bool(doc and doc.is_active),
            "requires_operator_reconciliation": request.state in {"running", "uncertain", "dispatch_failed"},
            "usage": usage_report(UsageCapture(str(organization.pk), set(request.usage_reservation_ids), request.usage_truncated))}


def reconcile_request(*, organization, request_id):
    """Confirm already persisted index completion without any provider retry."""
    from apps.ai_engagement.models import AIKnowledgeRepair, Document

    with transaction.atomic():
        request = AIKnowledgeRepair.objects.select_for_update().filter(pk=request_id, organization=organization).first()
        if request is None:
            raise SourceRepairError("repair_not_in_organization")
        if request.state not in {"running", "uncertain", "retrying"}:
            return repair_report(organization=organization, request_id=request.pk)
        document = Document.objects.filter(pk=request.result_document_id or request.document_id,
                                            organization=organization, source_key=request.document.source_key).first()
        chunks = document.chunks.filter(is_active=True) if document else None
        if (document is not None and document.processing_status == "completed" and chunks.exists()
                and not chunks.exclude(organization=organization).exists()
                and not chunks.filter(embedding__isnull=True).exists()):
            request.state = "completed"
            request.result_document_id = document.pk
            request.outcome_code = "reconciled_persisted_index"
            # A killed process can lose its context-local usage attribution.
            # Never label its missing reservation history as a verified zero.
            request.usage_truncated = True
            request.save(update_fields=["state", "result_document_id", "outcome_code", "usage_truncated", "updated_at"])
    return repair_report(organization=organization, request_id=request.pk)
