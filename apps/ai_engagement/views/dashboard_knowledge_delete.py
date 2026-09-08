from __future__ import annotations

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_POST

from apps.ai_engagement.models import Document, KnowledgeSource
from apps.ai_engagement.services.knowledge_source import (
    KnowledgeSourceService,
    KnowledgeSourceServiceError,
)
from apps.crm.authentication import crm_login_required


@require_POST
@crm_login_required
def dashboard_knowledge_delete(request, kind, item_id):
    """Permanently delete AI knowledge from the authenticated CRM org."""

    organization = request.crm_user.organization
    service = KnowledgeSourceService()

    try:
        if kind == "source":
            source = get_object_or_404(
                KnowledgeSource,
                id=item_id,
                organization=organization,
            )
            service.delete_source(source=source)

        elif kind == "document":
            document = get_object_or_404(
                Document,
                id=item_id,
                organization=organization,
            )
            service.delete_document(document=document)

        else:
            return JsonResponse(
                {"detail": "Unsupported knowledge item type."},
                status=400,
            )

    except KnowledgeSourceServiceError as exc:
        return JsonResponse(
            {"detail": str(exc)},
            status=400,
        )

    return JsonResponse({"deleted": True})
