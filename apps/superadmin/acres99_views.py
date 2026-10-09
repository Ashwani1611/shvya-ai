"""SHVYA Superadmin-owned 99acres connector provisioning."""
from __future__ import annotations

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.crm.models import Pipeline, Stage
from apps.integrations.acres99_models import Acres99Integration
from apps.organizations.models import Organization
from apps.superadmin.models import AuditLog
from apps.superadmin.views_flat import superuser_required


@superuser_required
@require_http_methods(["GET", "POST"])
def organization_acres99_view(request, organization_id):
    organization = get_object_or_404(Organization, pk=organization_id)
    connection = Acres99Integration.objects.filter(
        organization=organization
    ).select_related("pipeline", "stage").first()

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "sync_now":
            if not connection or not connection.is_enabled or connection.mode not in {"pull", "both"}:
                messages.error(request, "Enable 99acres Pull before running a sync.")
            else:
                from apps.integrations.tasks import sync_acres99_connection_task
                sync_acres99_connection_task.delay(str(connection.pk), manual=True)
                messages.success(request, "A 99acres Pull sync was queued.")
            return redirect("superadmin-organization-99acres", organization_id=organization.pk)

        if action not in {"generate", "rotate", "save", "disable"}:
            return render(request, "superadmin/acres99_setup.html", {
                "organization": organization, "integration": connection, "pipelines": [],
                "form_error": "Unsupported setup action.",
            }, status=400)

        if action == "disable":
            if connection:
                connection.is_enabled = False
                connection.save(update_fields=["is_enabled", "updated_at"])
                AuditLog.record(
                    actor=request.user, request=request,
                    target=organization, action=AuditLog.Action.ORGANIZATION_UPDATED,
                    operation="99acres_disabled",
                )
            messages.success(request, "99acres connector paused.")
            return redirect("superadmin-organization-99acres", organization_id=organization.pk)

        pipeline = get_object_or_404(
            Pipeline, pk=request.POST.get("pipeline_id"), organization=organization, is_active=True,
        )
        stage = get_object_or_404(
            Stage, pk=request.POST.get("stage_id"), pipeline=pipeline, is_active=True,
        )
        if connection is None:
            connection = Acres99Integration(organization=organization)
        connection.pipeline = pipeline
        connection.stage = stage
        connection.mode = request.POST.get("mode", "push")
        if connection.mode not in {c.value for c in Acres99Integration.Mode}:
            messages.error(request, "Select a valid 99acres connection method.")
            return redirect("superadmin-organization-99acres", organization_id=organization.pk)

        username = request.POST.get("account_username", "").strip()
        password = request.POST.get("account_password", "")
        if bool(username) != bool(password):
            messages.error(request, "Enter both username and password, or leave both blank.")
            return redirect("superadmin-organization-99acres", organization_id=organization.pk)
        if username and password:
            connection.set_credentials(username, password)
        endpoint_token = request.POST.get("provider_token", "").strip()
        if endpoint_token:
            try:
                connection.set_provider_token(endpoint_token)
            except ValidationError as exc:
                messages.error(request, "; ".join(exc.messages)[:500])
                return redirect("superadmin-organization-99acres", organization_id=organization.pk)
        token_changed = action in {"generate", "rotate"}
        if token_changed:
            connection.generate_webhook_token()
        connection.is_enabled = (
            True if token_changed else request.POST.get("is_enabled") == "on"
        )
        try:
            connection.full_clean()
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages)[:500])
            return redirect("superadmin-organization-99acres", organization_id=organization.pk)
        connection.save()
        AuditLog.record(
            actor=request.user, request=request,
            target=organization, action=AuditLog.Action.ORGANIZATION_UPDATED,
            operation=f"99acres_{action}", integration_id=str(connection.pk),
            mode=connection.mode, token_changed=token_changed,
            enabled=connection.is_enabled,
            pipeline_id=str(pipeline.pk), stage_id=str(stage.pk),
            pull_credentials_changed=bool(username and password),
            provider_token_changed=bool(endpoint_token),
        )
        messages.success(
            request,
            "99acres connection saved. Previous webhook URLs are revoked." if action == "rotate"
            else "99acres configuration updated.",
        )
        return redirect("superadmin-organization-99acres", organization_id=organization.pk)

    pipelines = Pipeline.objects.filter(
        organization=organization, is_active=True
    ).prefetch_related("stages").order_by("name")
    webhook_url = (
        request.build_absolute_uri(
            reverse("acres99-webhook", kwargs={"token": connection.webhook_token})
        )
        if connection and connection.webhook_token else ""
    )
    response = render(request, "superadmin/acres99_setup.html", {
        "organization": organization,
        "integration": connection,
        "pipelines": pipelines,
        "webhook_url": webhook_url,
        "recent_events": connection.events.select_related("lead").all()[:20] if connection else [],
    })
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    return response
