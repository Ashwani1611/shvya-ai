"""Superadmin-only organization model configuration."""
from django.contrib import messages
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from apps.ai_engagement.services.org_info import MODEL_ROUTING_FIELDS, OrgInfoService, OrgInfoServiceError
from apps.organizations.models import Organization
from .models import AuditLog
from .views_flat import superuser_required


@superuser_required
@require_POST
def organization_model_routing_update_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    data = {field: request.POST.get(field, "").strip() for field in sorted(MODEL_ROUTING_FIELDS)}
    try:
        with transaction.atomic():
            Organization.objects.select_for_update().get(pk=organization.pk)
            service = OrgInfoService()
            info = service.get_or_create(organization=organization)
            before = {field: getattr(info, field) for field in data}
            service.update(organization=organization, data=data, allow_model_routing=True)
            AuditLog.record(
                actor=request.user, action=AuditLog.Action.ORGANIZATION_UPDATED,
                target=organization, request=request,
                operation="ai_model_routing", before=before, after=data,
            )
    except OrgInfoServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Organization AI model routing updated.")
    return redirect(reverse("superadmin-organization-detail", kwargs={"organization_id": organization.pk}) + "#ai-model-routing")
