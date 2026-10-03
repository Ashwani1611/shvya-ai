"""Plan explicit repairs and dispatch the existing bounded ingestion tasks.

Never run from a customer message or automatically reactivate retired sources.
A revision-bound request is durable; retrying broker publication uses the same
canonical task identity. Existing ingestion tasks remain the sole retry owners.
"""
from __future__ import annotations

import hashlib
import json
import logging
from uuid import UUID
from functools import wraps
from contextvars import ContextVar

from django.db import DatabaseError, transaction
from django.db.models import BooleanField, Case, Value, When
from django.utils import timezone

from apps.ai_engagement.models import Chunk, Document, KnowledgeRepairRequest, KnowledgeSource


logger = logging.getLogger(__name__)
MAX_PLAN_CHUNKS = 5000
_CURRENT_REPAIR: ContextVar = ContextVar("shvya_explicit_knowledge_repair", default=None)
OPERATIONS = {"reindex_missing", "retry_upload", "refresh_url"}


class KnowledgeRepairError(ValueError):
    pass


def plan_repair(*, organization, document_id) -> dict:
    document = Document.objects.filter(pk=document_id, organization=organization).first()
    if document is None:
        raise KnowledgeRepairError("document_not_found")
    source = None
    if document.source_url:
        source = KnowledgeSource.objects.filter(organization=organization, source_type="url",
            url=document.source_url, is_active=True).order_by("pk").first()
    rows = list(Chunk.objects.filter(document=document).annotate(embedded=Case(
        When(embedding__isnull=False, then=Value(True)), default=Value(False), output_field=BooleanField(),
    )).order_by("pk").values("id", "organization_id", "content", "is_active", "embedded")[:MAX_PLAN_CHUNKS + 1])
    # Never hash or return raw vectors; only their presence affects missing-only repair.
    chunk_state = [(row["id"], str(row["organization_id"]), row["is_active"], row["embedded"],
                    hashlib.sha256(row["content"].encode("utf-8")).hexdigest()) for row in rows]
    latest = Document.objects.filter(organization=organization, source_key=document.source_key).order_by(
        "-version", "-pk").values_list("pk", flat=True).first() if document.source_key else document.pk
    state = {"id": document.pk, "organization": str(organization.pk), "latest": latest,
        "source_key": document.source_key, "version": document.version, "file": document.file.name,
        "source_url": document.source_url, "status": document.processing_status,
        "active": document.is_active, "updated_at": document.updated_at.isoformat(), "chunks": chunk_state,
        "source": (source.pk, source.updated_at.isoformat()) if source else None}
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True).encode("utf-8")).hexdigest()
    active = [row for row in rows if row["is_active"]]
    missing = sum(not row["embedded"] for row in active)
    operation, reason = None, "healthy_or_no_repair_needed"
    if not getattr(organization, "is_active", True):
        reason = "organization_inactive"
    elif len(rows) > MAX_PLAN_CHUNKS:
        reason = "source_requires_manual_review"
    elif any(str(row["organization_id"]) != str(organization.pk) for row in rows):
        reason = "chunk_tenant_mismatch"
    elif latest != document.pk:
        reason = "superseded_document"
    elif document.source_url and source is None:
        reason = "source_disabled_or_missing"
    elif document.processing_status not in {"completed", "failed"}:
        reason = "source_busy"
    elif document.processing_status == "completed" and not document.is_active:
        reason = "document_inactive"
    elif any(not row["content"].strip() for row in active):
        reason = "empty_chunks_require_manual_review"
    elif active and (missing or document.processing_status == "failed") and document.source_key:
        operation, reason = "reindex_missing", "missing_or_failed_embeddings"
    elif not active and document.file:
        operation, reason = "retry_upload", "missing_extracted_content"
    elif not active and source:
        operation, reason = "refresh_url", "missing_extracted_content"
    return {"document_id": document.pk, "fingerprint": fingerprint, "operation": operation,
            "reason": reason, "source_id": source.pk if source else None,
            "active_chunk_count": len(active), "missing_embedding_count": missing,
            "requires_explicit_apply": True, "may_consume_ai_credits": operation is not None}


def request_repair(*, organization, document_id, expected_fingerprint):
    with transaction.atomic():
        # Lock the same tenant row as canonical source versioning/publication.
        locked_org = type(organization).objects.select_for_update().get(pk=organization.pk)
        plan = plan_repair(organization=locked_org, document_id=document_id)
        if not expected_fingerprint or plan["fingerprint"] != expected_fingerprint:
            raise KnowledgeRepairError("repair_plan_changed")
        if plan["operation"] not in OPERATIONS:
            raise KnowledgeRepairError(plan["reason"])
        request, created = KnowledgeRepairRequest.objects.get_or_create(
            organization=locked_org, document_id=document_id, fingerprint=expected_fingerprint,
            defaults={"operation": plan["operation"], "source_id": plan["source_id"]})
        if created:
            transaction.on_commit(lambda: dispatch_repair(organization_id=locked_org.pk, request_id=request.pk))
    return request


def dispatch_repair(*, organization_id, request_id):
    from apps.ai_engagement import tasks
    request = KnowledgeRepairRequest.objects.filter(pk=request_id, organization_id=organization_id).first()
    if request is None:
        raise KnowledgeRepairError("repair_request_not_found")
    if request.status not in {"queued", "dispatch_failed"}:
        return request.status
    kwargs = {"organization_id": str(request.organization_id)}
    if request.operation == "refresh_url":
        if not request.source_id:
            raise KnowledgeRepairError("source_disabled_or_missing")
        task = tasks.ingest_and_index_url_source
        kwargs["source_id"] = request.source_id
    elif request.operation == "reindex_missing":
        task = tasks.reindex_document_embeddings
        kwargs.update(document_id=request.document_id, only_missing=True)
    elif request.operation == "retry_upload":
        task = tasks.ingest_and_index_document
        kwargs["document_id"] = request.document_id
    else:
        raise KnowledgeRepairError("unsupported_repair")
    try:
        task.apply_async(kwargs=kwargs, task_id=str(request.task_id))
    except Exception:
        # Publication failure remains durable and can be explicitly redispatched.
        _dispatch_status(request, failed=True)
        return "dispatch_failed"
    _dispatch_status(request, failed=False)
    return "queued"


def _dispatch_status(request, *, failed):
    try:
        with transaction.atomic():
            query = KnowledgeRepairRequest.objects.filter(pk=request.pk, organization_id=request.organization_id)
            if failed:
                query.filter(status__in=["queued", "dispatch_failed"]).update(status="dispatch_failed",
                    outcome_code="broker_publication_failed", updated_at=timezone.now())
            else:
                query.filter(status="dispatch_failed").update(status="queued", outcome_code="",
                    updated_at=timezone.now())
    except DatabaseError:
        # Same task ID + the transactional claim protects an explicit redispatch.
        logger.warning("knowledge_repair_dispatch_status_unavailable request=%s", request.pk)



def _record_outcome(request, **fields):
    # A successful provider operation must not be turned into a retry because
    # optional request-status persistence failed. RUNNING then requires review.
    try:
        with transaction.atomic():
            KnowledgeRepairRequest.objects.filter(pk=request.pk, organization_id=request.organization_id,
                status="running", attempt=request.attempt).update(**fields, updated_at=timezone.now())
    except DatabaseError:
        logger.warning("knowledge_repair_outcome_unavailable request=%s", request.pk)


def repair_publication_allowed(document):
    """Additional restriction for explicit repair jobs, under publication's lock.

    Ordinary publication remains unchanged. A disabled/deleted URL source must
    not be resurrected because a repair's network request completed late.
    """
    request = _CURRENT_REPAIR.get()
    if request is None:
        return True
    if document.organization_id != request.organization_id:
        return False
    if request.operation != "refresh_url" and document.pk != request.document_id:
        return False
    if request.source_id:
        return KnowledgeSource.objects.select_for_update().filter(pk=request.source_id,
            organization_id=request.organization_id, source_type="url", is_active=True,
            url=request.document.source_url).exists()
    return not document.source_url


def _verified_result(request, result):
    """Completed work is not a successful repair until its index is published."""
    document_id = result.get("document_id")
    if type(document_id) is not int or document_id <= 0:
        return "failed", "missing_result_document", None
    document = Document.objects.filter(pk=document_id, organization_id=request.organization_id).first()
    if document is None or (request.operation != "refresh_url" and document_id != request.document_id):
        return "failed", "result_scope_mismatch", None
    if request.operation == "refresh_url" and document.source_url != request.document.source_url:
        return "failed", "result_scope_mismatch", None
    if document.processing_status != "completed" or not document.is_active:
        return "skipped", "result_not_published", document_id
    chunks = Chunk.objects.filter(document=document, is_active=True)
    if (not chunks.exists() or chunks.exclude(organization_id=request.organization_id).exists()
            or chunks.filter(embedding__isnull=True).exists()):
        return "failed", "result_index_incomplete", document_id
    return "succeeded", "published_index_verified", document_id


def tracked_repair_task(function):
    """Guard explicit repair IDs; keep the canonical task's retry ownership.

    A crashed RUNNING request requires review, never a blind automatic replay.
    Ordinary ingestion without a repair request runs outside this transaction.
    """
    @wraps(function)
    def run(task, *args, **kwargs):
        from celery.exceptions import Retry
        task_id = getattr(task.request, "id", None)
        try:
            task_id = UUID(str(task_id))
        except (TypeError, ValueError):
            return function(task, *args, **kwargs)
        with transaction.atomic():
            request = KnowledgeRepairRequest.objects.select_for_update().filter(task_id=task_id).first()
            if request is not None:
                expected_task = {"refresh_url": "ingest_and_index_url_source",
                    "reindex_missing": "reindex_document_embeddings", "retry_upload": "ingest_and_index_document"}
                if request.document.source_url and (not request.source_id or not KnowledgeSource.objects.filter(
                        pk=request.source_id, organization_id=request.organization_id, is_active=True,
                        source_type="url", url=request.document.source_url).exists()):
                    request.status, request.outcome_code = "skipped", "source_disabled_or_missing"
                    request.save(update_fields=["status", "outcome_code", "updated_at"])
                    return {"status": "skipped", "reason": "source_disabled_or_missing"}
                if (args or expected_task.get(request.operation) != function.__name__
                        or str(kwargs.get("organization_id")) != str(request.organization_id)
                        or (request.operation != "refresh_url" and kwargs.get("document_id") != request.document_id)
                        or (request.operation == "refresh_url" and kwargs.get("source_id") != request.source_id)
                        or (request.operation == "reindex_missing" and kwargs.get("only_missing") is not True)):
                    raise KnowledgeRepairError("repair_task_scope_mismatch")
                if request.status not in {"queued", "dispatch_failed", "retrying"}:
                    return {"status": "skipped", "reason": "repair_already_claimed_or_finished"}
                retries = int(task.request.retries)
                if request.attempt and retries < request.attempt:
                    return {"status": "skipped", "reason": "stale_repair_attempt"}
                if not request.organization.is_active:
                    request.status, request.outcome_code = "skipped", "organization_inactive"
                    request.save(update_fields=["status", "outcome_code", "updated_at"])
                    return {"status": "skipped", "reason": "organization_inactive"}
                if request.attempt == 0:
                    plan = plan_repair(organization=request.organization, document_id=request.document_id)
                    if plan["fingerprint"] != request.fingerprint or plan["operation"] != request.operation:
                        request.status, request.outcome_code = "skipped", "repair_plan_changed"
                        request.save(update_fields=["status", "outcome_code", "updated_at"])
                        return {"status": "skipped", "reason": "repair_plan_changed"}
                request.status, request.attempt = "running", retries + 1
                request.save(update_fields=["status", "attempt", "updated_at"])
        if request is None:
            return function(task, *args, **kwargs)
        token = _CURRENT_REPAIR.set(request)
        try:
            result = function(task, *args, **kwargs)
        except Retry:
            _record_outcome(request, status="retrying", outcome_code="canonical_task_retry")
            raise
        except Exception:
            _record_outcome(request, status="failed", outcome_code="canonical_task_failed")
            raise
        finally:
            _CURRENT_REPAIR.reset(token)
        result = result if isinstance(result, dict) else {}
        status = {"completed": "succeeded", "failed": "failed", "skipped": "skipped"}.get(result.get("status"), "failed")
        document_id = result.get("document_id")
        if type(document_id) is not int or document_id <= 0:
            document_id = None
        outcome = "canonical_" + status
        if status == "succeeded":
            try:
                with transaction.atomic():
                    status, outcome, document_id = _verified_result(request, result)
            except DatabaseError:
                logger.warning("knowledge_repair_verification_unavailable request=%s", request.pk)
                return result  # RUNNING requests require review; do not replay success.
        _record_outcome(request, status=status, outcome_code=outcome,
                        result_document_id=document_id)
        return result
    return run
