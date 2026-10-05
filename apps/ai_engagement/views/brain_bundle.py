"""Authorized downloads of the authenticated organization's saved AI Brain."""
from __future__ import annotations

import json
import re

from django.http import HttpResponse
from django.utils.http import content_disposition_header
from django.views.decorators.http import require_safe
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.views import APIView

from apps.ai_engagement.services.organization_brain_bundle import (
    get_organization_ai_brain_bundle,
)
from apps.crm.authentication import crm_login_required
from apps.organizations.access import crm_user_is_authorized


def _bundle_download_response(*, organization):
    bundle = get_organization_ai_brain_bundle(organization=organization)
    # The builder owns the revision. Keep HTTP filenames safe even if the
    # manifest format changes; organization names never enter the header.
    revision = re.sub(r"[^a-zA-Z0-9_-]", "", str(bundle["revision"]))[:12]
    filename = f"shvya-ai-brain-{organization.pk}-{revision}.json"
    response = HttpResponse(
        json.dumps(bundle, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        content_type="application/json; charset=utf-8",
    )
    response["Content-Disposition"] = content_disposition_header(
        as_attachment=True, filename=filename,
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@crm_login_required
@require_safe
def dashboard_ai_brain_download(request):
    """Use the dedicated CRM session, matching existing AI Brain access."""
    return _bundle_download_response(organization=request.crm_user.organization)


class IsAIBrainAuthor(BasePermission):
    """Match the active organization-user boundary of the AI Brain page."""

    def has_permission(self, request, view):
        return crm_user_is_authorized(request.user)


class OrganizationAIBrainBundleView(APIView):
    permission_classes = [IsAuthenticated, IsAIBrainAuthor]

    def get(self, request):
        # Organization IDs in the URL/query/body are never accepted as scope.
        return _bundle_download_response(organization=request.user.organization)
