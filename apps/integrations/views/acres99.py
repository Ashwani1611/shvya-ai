"""99acres Connect Hub setup page and XML Push callback."""
from __future__ import annotations

import logging
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from apps.integrations.access import connect_hub_admin_required
from apps.integrations.acres99_models import Acres99Integration
from apps.integrations.services.acres99 import (
    Acres99ProtocolError, ingest_query, parse_push_xml, push_acknowledgement,
    record_failure, MAX_PUSH_BYTES,
)

logger = logging.getLogger(__name__)


@connect_hub_admin_required
@require_http_methods(["GET", "POST"])
def acres99_connect_view(request):
    organization = request.crm_user.organization
    integration = Acres99Integration.objects.filter(
        organization=organization
    ).select_related("pipeline", "stage").first()

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        if action == "request_setup":
            if integration is None:
                Acres99Integration.objects.create(organization=organization)
                messages.success(request, "99acres setup requested. SHVYA will provision your connector.")
            else:
                messages.info(request, "Your 99acres request is already registered.")
        elif action == "sync_now":
            if (
                integration is None or not integration.is_enabled
                or integration.mode not in {"pull", "both"}
                or not integration.has_credentials or not integration.has_provider_token
            ):
                return HttpResponse("99acres Pull is not enabled.", status=400)
            from apps.integrations.tasks import sync_acres99_connection_task
            sync_acres99_connection_task.delay(str(integration.pk), manual=True)
            messages.success(request, "99acres sync queued. Check recent activity for the result.")
        else:
            return HttpResponse("Unsupported setup action.", status=400)
        return redirect("crm-connect-hub-99acres")

    webhook_url = ""
    if integration and integration.webhook_token:
        webhook_url = request.build_absolute_uri(
            reverse("acres99-webhook", kwargs={"token": integration.webhook_token})
        )
    response = render(request, "integrations/acres99.html", {
        "integration": integration,
        "webhook_url": webhook_url,
        "recent_events": (
            integration.events.select_related("lead").all()[:12] if integration else []
        ),
    })
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "same-origin"
    return response


@csrf_exempt
@require_POST
def acres99_push_view(request, token):
    """Acknowledge accepted Query IDs, but never mark a failed lead successful.

    An N response for failures is a conservative extension of the vendor's
    documented Y acknowledgement. Confirm failure/retry rules with 99acres.
    """
    integration = Acres99Integration.objects.filter(
        webhook_token=token,
        is_enabled=True,
        mode__in=[Acres99Integration.Mode.PUSH, Acres99Integration.Mode.BOTH],
        organization__is_active=True,
    ).first()
    if integration is None:
        return HttpResponse("Webhook unavailable.", status=404)
    if len(request.body) > MAX_PUSH_BYTES:
        return HttpResponse("Payload too large.", status=413)
    # XML is normally delivered raw. Accept the named form field too, for
    # providers that wrap XML in an application/x-www-form-urlencoded POST.
    if request.content_type in {"application/x-www-form-urlencoded", "multipart/form-data"}:
        raw = request.POST.get("xml", "").encode("utf-8")
    else:
        raw = request.body
    try:
        queries = parse_push_xml(raw)
    except Acres99ProtocolError:
        return HttpResponse("Invalid 99acres XML.", status=400)
    answers = []
    for query in queries:
        try:
            ingest_query(integration, query, direction="push")
            answers.append((str(query.get("query_id") or ""), True))
        except (ValidationError, IntegrityError, Acres99ProtocolError) as exc:
            try:
                record_failure(integration, query, "push", type(exc).__name__)
            except Exception:
                logger.exception("Could not record 99acres rejected enquiry.")
            answers.append((str(query.get("query_id") or ""), False))
        except Exception:
            logger.exception("Unexpected 99acres Push processing error for %s", integration.pk)
            try:
                record_failure(integration, query, "push", "processing_error")
            except Exception:
                logger.exception("Could not record 99acres processing error.")
            answers.append((str(query.get("query_id") or ""), False))
    response = HttpResponse(
        push_acknowledgement(answers),
        status=200 if all(ok for _, ok in answers) else 503,
        content_type="application/xml; charset=utf-8",
    )
    response["Cache-Control"] = "no-store"
    return response
