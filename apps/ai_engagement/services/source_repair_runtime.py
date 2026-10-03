"""Attach repair receipts to canonical Celery tasks without another retry owner."""
from __future__ import annotations

import logging
from contextvars import ContextVar
from functools import wraps

from apps.ai_engagement.services.source_repair import SourceRepairError, inspect_document


logger = logging.getLogger(__name__)
_CURRENT_REPAIR = ContextVar("shvya_source_repair", default=None)


def preserve_repair_retry(kwargs):
    """Keep exact URL version and repair identity through existing retry calls."""
    request_id = _CURRENT_REPAIR.get()
    if request_id:
        from apps.ai_engagement.models import AIKnowledgeRepair
        kwargs["repair_request_id"] = request_id
        request = AIKnowledgeRepair.objects.filter(pk=request_id, organization_id=kwargs.get("organization_id")).first()
        if request and request.action == "reindex_missing":
            kwargs["only_missing"] = True
        if request and kwargs.get("document_id"):
            AIKnowledgeRepair.objects.filter(pk=request.pk).update(result_document_id=kwargs["document_id"])
    return kwargs



def bind_repair_document(document):
    request_id = _CURRENT_REPAIR.get()
    if request_id:
        from apps.ai_engagement.models import AIKnowledgeRepair
        AIKnowledgeRepair.objects.filter(pk=request_id, organization_id=document.organization_id,
            document__source_key=document.source_key, state="running").update(result_document_id=document.pk)

def _claim(*, task, action, kwargs):
    from django.db import transaction
    from django.utils import timezone
    from apps.ai_engagement.models import AIKnowledgeRepair, Document
    from apps.organizations.models import Organization

    organization_id = kwargs.get("organization_id")
    with transaction.atomic():
        org = Organization.objects.select_for_update().filter(pk=organization_id, is_active=True).first()
        if org is None:
            raise SourceRepairError("organization_inactive_or_missing")
        initial = AIKnowledgeRepair.objects.filter(pk=kwargs["repair_request_id"], organization=org).first()
        if initial is None:
            raise SourceRepairError("repair_not_in_organization")
        document = Document.objects.select_for_update().get(pk=initial.document_id, organization=org)
        request = AIKnowledgeRepair.objects.select_for_update().get(pk=initial.pk, organization=org)
        retries = int(task.request.retries or 0)
        if request.action != action or str(task.request.id or request.pk) != str(request.pk):
            raise SourceRepairError("repair_task_identity_mismatch")
        if request.source_version != document.version:
            raise SourceRepairError("repair_source_version_changed")
        expected_document = request.result_document_id or request.document_id
        if action == "retry_url":
            source = request.source
            if (source is None or source.organization_id != org.pk or not source.is_active
                    or str(source.url).strip() != document.source_key
                    or kwargs.get("source_id") != source.pk):
                raise SourceRepairError("repair_url_source_changed")
            if kwargs.get("document_id") is not None and kwargs["document_id"] != expected_document:
                raise SourceRepairError("repair_document_identity_mismatch")
        elif kwargs.get("document_id") != expected_document:
            raise SourceRepairError("repair_document_identity_mismatch")
        if action == "reindex_missing" and kwargs.get("only_missing") is not True:
            raise SourceRepairError("repair_must_preserve_completed_embeddings")
        if request.state in {"queued", "dispatch_failed"} and request.last_attempt == -1:
            current = inspect_document(organization=org, document_id=document.pk)
            if current["document_fingerprint"] != request.document_fingerprint:
                request.state, request.outcome_code = "stale", "source_changed_before_repair"
                request.save(update_fields=["state", "outcome_code", "updated_at"])
                return None
        elif request.state == "retrying" and retries > request.last_attempt:
            pass
        else:
            return None
        request.state, request.last_attempt, request.outcome_code = "running", retries, ""
        request.updated_at = timezone.now()
        request.save(update_fields=["state", "last_attempt", "outcome_code", "updated_at"])
        return request


def _record(*, request, state, code, capture=None, result=None):
    from django.db import transaction
    from apps.ai_engagement.models import AIKnowledgeRepair, Document
    from apps.ai_engagement.services.usage_attribution import MAX_RESERVATIONS

    with transaction.atomic():
        locked = AIKnowledgeRepair.objects.select_for_update().get(pk=request.pk, organization_id=request.organization_id)
        if locked.last_attempt != request.last_attempt or locked.state != "running":
            return
        ids = set(locked.usage_reservation_ids if isinstance(locked.usage_reservation_ids, list) else [])
        if capture:
            ids.update(capture.reservation_ids)
            locked.usage_truncated |= capture.truncated or len(ids) > MAX_RESERVATIONS
        locked.usage_reservation_ids = sorted(ids)[:MAX_RESERVATIONS]
        if state == "completed":
            from apps.ai_engagement.services.file_delivery_receipts import positive_id
            document_id = positive_id((result or {}).get("document_id")) or locked.result_document_id or locked.document_id
            document = Document.objects.filter(pk=document_id, organization_id=request.organization_id,
                                                source_key=request.document.source_key).first()
            chunks = document.chunks.filter(is_active=True) if document else None
            if (document is None or document.processing_status != "completed" or not chunks.exists()
                    or chunks.exclude(organization_id=request.organization_id).exists()
                    or chunks.filter(embedding__isnull=True).exists()):
                state, code = "failed", "repair_verification_failed"
            else:
                locked.result_document_id = document.pk
                code = "indexed_and_published" if document.is_active else "index_repaired_not_published"
        locked.state, locked.outcome_code = state, code
        locked.save(update_fields=["state", "outcome_code", "result_document_id", "usage_reservation_ids",
                                   "usage_truncated", "updated_at"])


def _safe_record(**kwargs):
    try:
        _record(**kwargs)
    except Exception:
        # A receipt write must not cause successful external indexing to replay.
        logger.warning("Knowledge repair outcome needs reconciliation request=%s", kwargs["request"].pk)


def tracked_repair(action):
    """Only approved repair invocations are wrapped; legacy ingestion is unchanged."""
    def decorate(original):
        @wraps(original)
        def run(task, *args, **kwargs):
            if not kwargs.get("repair_request_id"):
                return original(task, *args, **kwargs)
            if args:
                raise SourceRepairError("repair_requires_explicit_keyword_identity")
            request = _claim(task=task, action=action, kwargs=kwargs)
            if request is None:
                return {"status": "skipped", "reason": "duplicate_or_stale_repair"}
            from celery.exceptions import Retry
            from apps.ai_engagement.services.usage_attribution import capture_usage
            token = _CURRENT_REPAIR.set(str(request.pk))
            captured = None
            try:
                with capture_usage(request.organization_id) as captured:
                    result = original(task, **kwargs)
                status = result.get("status") if isinstance(result, dict) else None
                state = "completed" if status == "completed" else "failed"
                _safe_record(request=request, state=state, code="canonical_task_failed" if state == "failed" else "",
                             result=result, capture=captured)
                return result
            except Retry:
                _safe_record(request=request, state="retrying", code="canonical_retry_scheduled", capture=captured)
                raise
            except Exception:
                _safe_record(request=request, state="uncertain", code="processing_outcome_uncertain", capture=captured)
                raise
            finally:
                _CURRENT_REPAIR.reset(token)
        return run
    return decorate
