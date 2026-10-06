from __future__ import annotations

import json
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.integrations.access import connect_hub_admin_required
from apps.integrations.justdial_models import JustDialIntegration
from apps.integrations.services.justdial import (
    process_justdial_lead,
    record_justdial_failure,
)

logger = logging.getLogger(__name__)


def _request_payload(request):
    if request.method == "GET":
        return request.GET.dict()

    content_type = str(request.content_type or "").lower()
    if "application/json" in content_type:
        try:
            payload = json.loads(request.body or b"{}")
        except (TypeError, ValueError):
            raise ValidationError("Invalid JSON payload.")
        if not isinstance(payload, dict):
            raise ValidationError("JustDial webhook payload must be an object.")
        return payload

    return request.POST.dict()


@connect_hub_admin_required
@require_http_methods(["GET", "POST"])
def justdial_connect_view(request):
    organization = request.crm_user.organization
    integration = JustDialIntegration.objects.filter(
        organization=organization
    ).first()

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action != "request_setup":
            return HttpResponse("Unsupported action", status=400)

        if integration is None:
            integration = JustDialIntegration.objects.create(
                organization=organization,
            )
            messages.success(
                request,
                "JustDial setup requested. Our team will provision the webhook for your organization.",
            )
        elif integration.webhook_token:
            messages.info(
                request,
                "Your JustDial webhook has already been provisioned by SHVYA.",
            )
        else:
            messages.info(
                request,
                "Your JustDial setup request is already with the SHVYA team.",
            )
        return redirect("crm-connect-hub-justdial")

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
        recent_events = integration.events.select_related("lead").all()[:8]

    return render(
        request,
        "integrations/justdial.html",
        {
            "integration": integration,
            "webhook_url": webhook_url,
            "recent_events": recent_events,
        },
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
def justdial_webhook_view(request, token):
    """Receive a JustDial lead push.

    JustDial CRM connectors are commonly provisioned as provider-side GET
    callbacks. SHVYA treats GET as canonical while also accepting form-encoded
    and JSON POST payloads so provider/account configurations can be migrated
    without changing the CRM ingestion contract.
    """
    integration = (
        JustDialIntegration.objects.select_related(
            "organization",
            "pipeline",
            "stage",
        )
        .filter(webhook_token=token)
        .first()
    )
    if integration is None:
        return HttpResponse("NOT_FOUND", status=404)

    try:
        payload = _request_payload(request)
    except ValidationError:
        return HttpResponse("INVALID_PAYLOAD", status=400)

    try:
        event, lead, created = process_justdial_lead(
            integration=integration,
            payload=payload,
            method=request.method,
        )
    except ValidationError:
        return HttpResponse("INVALID_LEAD", status=422)
    except Exception as exc:
        logger.exception(
            "Unexpected JustDial webhook failure for integration %s",
            integration.pk,
        )
        try:
            record_justdial_failure(
                integration=integration,
                payload=payload,
                method=request.method,
                error_message=f"Unexpected processing failure: {type(exc).__name__}",
            )
        except Exception:
            logger.exception(
                "Could not persist JustDial failure event for integration %s",
                integration.pk,
            )
        return HttpResponse("RETRY", status=500)

    if event.status == event.Status.IGNORED:
        return HttpResponse("IGNORED", status=200)

    return HttpResponse(
        "CREATED" if created and lead is not None else "UPDATED",
        status=200,
    )
