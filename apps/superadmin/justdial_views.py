from __future__ import annotations

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.crm.models import Pipeline, Stage
from apps.integrations.justdial_models import JustDialIntegration
from apps.organizations.models import Organization

from .models import AuditLog
from .views_flat import superuser_required


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_justdial_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    integration = JustDialIntegration.objects.filter(
        organization=organization
    ).select_related("pipeline", "stage").first()

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action not in {"generate", "rotate", "save"}:
            messages.error(request, "Unsupported JustDial setup action.")
            return redirect(
                "superadmin-organization-justdial",
                organization_id=organization.id,
            )

        pipeline = get_object_or_404(
            Pipeline,
            pk=request.POST.get("pipeline_id"),
            organization=organization,
            is_active=True,
        )
        stage = get_object_or_404(
            Stage,
            pk=request.POST.get("stage_id"),
            pipeline=pipeline,
            is_active=True,
        )

        if integration is None:
            integration = JustDialIntegration(
                organization=organization,
            )

        integration.pipeline = pipeline
        integration.stage = stage

        token_changed = False
        if action in {"generate", "rotate"}:
            integration.generate_webhook_token()
            integration.is_enabled = True
            token_changed = True
        else:
            integration.is_enabled = bool(
                integration.webhook_token
                and request.POST.get("is_enabled") == "on"
            )

        integration.full_clean()
        integration.save()

        AuditLog.record(
            actor=request.user,
            action=AuditLog.Action.ORGANIZATION_UPDATED,
            target=organization,
            request=request,
            operation=(
                "justdial_webhook_rotated"
                if action == "rotate"
                else "justdial_webhook_generated"
                if action == "generate"
                else "justdial_setup_updated"
            ),
            justdial_integration_id=str(integration.pk),
            pipeline_id=str(pipeline.pk),
            stage_id=str(stage.pk),
            token_changed=token_changed,
            enabled=integration.is_enabled,
        )

        if action == "rotate":
            messages.success(
                request,
                "JustDial webhook rotated. The previous URL is no longer valid.",
            )
        elif action == "generate":
            messages.success(
                request,
                "JustDial webhook generated and enabled for this organization.",
            )
        else:
            messages.success(request, "JustDial routing updated.")

        return redirect(
            "superadmin-organization-justdial",
            organization_id=organization.id,
        )

    pipelines = (
        Pipeline.objects.filter(
            organization=organization,
            is_active=True,
        )
        .prefetch_related("stages")
        .order_by("name")
    )
    webhook_url = ""
    recent_events = []
    if integration:
        if integration.webhook_token:
            webhook_url = request.build_absolute_uri(
                reverse(
                    "justdial-webhook",
                    kwargs={"token": integration.webhook_token},
                )
            )
        recent_events = integration.events.select_related("lead").all()[:25]

    return render(
        request,
        "superadmin/justdial_setup.html",
        {
            "organization": organization,
            "integration": integration,
            "pipelines": pipelines,
            "webhook_url": webhook_url,
            "recent_events": recent_events,
        },
    )
