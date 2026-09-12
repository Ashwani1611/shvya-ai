import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.contrib import messages
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.crm.authentication import crm_login_required
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.integrations.models import MetaLeadForm, MetaLeadPage
from services.crm.lead_service import upsert_lead

logger = logging.getLogger(__name__)


def _normalise_field_key(value):
    return " ".join(str(value or "").strip().casefold().split())


def _field_value(mapping, target, fields):
    """Return a mapped Meta field value, matching keys case-insensitively."""
    source = _normalise_field_key(mapping.get(target, ""))
    if not source:
        return ""

    for key, value in fields.items():
        if _normalise_field_key(key) == source:
            return str(value or "").strip()
    return ""


def _fetch_lead(leadgen_id, page):
    response = requests.get(
        f"https://graph.facebook.com/v23.0/{leadgen_id}",
        params={
            "fields": "field_data,ad_id,form_id,created_time",
            "access_token": page.get_page_access_token(),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


@csrf_exempt
def meta_lead_webhook(request):
    if request.method == "GET":
        expected = getattr(settings, "META_LEAD_VERIFY_TOKEN", "")
        if not expected or request.GET.get("hub.verify_token") != expected:
            return HttpResponse("Invalid verify token", status=403)
        return HttpResponse(request.GET.get("hub.challenge", ""))

    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        payload = json.loads(request.body or b"{}")
    except (TypeError, ValueError):
        return HttpResponse(status=400)

    entries = payload.get("entry", [])
    page_ids = [str(entry.get("id") or "") for entry in entries]
    pages_by_id = {
        page.page_id: page
        for page in MetaLeadPage.objects.filter(
            page_id__in=page_ids, is_active=True
        )
    }

    # Meta signs the entire request body once. Validate it before processing
    # any event, using the configured app secret for one of the subscribed pages.
    page = next(iter(pages_by_id.values()), None)
    secret = (
        page.get_app_secret() if page else ""
    ) or getattr(settings, "META_APP_SECRET", "")
    expected_signature = "sha256=" + hmac.new(
        secret.encode("utf-8"), request.body, hashlib.sha256
    ).hexdigest()
    if not secret or not hmac.compare_digest(
        request.headers.get("X-Hub-Signature-256", ""), expected_signature
    ):
        return HttpResponse(status=403)

    for entry in entries:
        page = pages_by_id.get(str(entry.get("id") or ""))
        if not page:
            logger.warning("Received Meta webhook for an unconfigured Page")
            continue

        for change in entry.get("changes", []):
            value = change.get("value") or {}
            leadgen_id = str(value.get("leadgen_id") or "")
            if change.get("field") != "leadgen" or not leadgen_id:
                continue

            try:
                lead_data = _fetch_lead(leadgen_id, page)
                form_id = str(lead_data.get("form_id") or value.get("form_id") or "")
                form = (
                    MetaLeadForm.objects.select_related("pipeline", "stage")
                    .filter(page=page, form_id=form_id, is_active=True)
                    .first()
                )
                if not form:
                    logger.warning("No active mapping for Meta form %s", form_id)
                    continue

                fields = {
                    item.get("name", ""): (item.get("values") or [""])[0]
                    for item in lead_data.get("field_data", [])
                    if item.get("name")
                }
                mapping = form.field_mapping or {}
                phone = _field_value(mapping, "phone", fields)
                if not phone:
                    logger.warning("Meta lead %s has no mapped phone", leadgen_id)
                    continue

                attributes = {
                    target.split(":", 1)[1]: _field_value(mapping, target, fields)
                    for target in mapping
                    if target.startswith("attribute:")
                    and _field_value(mapping, target, fields)
                }
                attributes.update(
                    {
                        "meta_lead_form": form.form_name,
                        "meta_lead_form_id": form.form_id,
                        "meta_leadgen_id": leadgen_id,
                        "meta_lead_ad_id": str(
                            lead_data.get("ad_id") or value.get("ad_id") or ""
                        ),
                    }
                )
                upsert_lead(
                    organization=page.organization,
                    pipeline=form.pipeline,
                    stage=form.stage,
                    name=_field_value(mapping, "name", fields) or "Meta Lead",
                    phone=phone,
                    email=_field_value(mapping, "email", fields),
                    attributes=attributes,
                    lead_source="meta_ads",
                )
            except Exception:
                logger.exception("Failed to import Meta lead %s", leadgen_id)

    return HttpResponse("EVENT_RECEIVED")


@crm_login_required
@require_GET
def meta_lead_forms_view(request):
    organization = request.crm_user.organization
    pages = (
        MetaLeadPage.objects.filter(organization=organization, is_active=True)
        .prefetch_related("forms")
        .order_by("page_name")
    )
    return render(
        request,
        "integrations/meta_lead_forms.html",
        {
            "pages": pages,
            "pipelines": Pipeline.objects.filter(
                organization=organization, is_active=True
            ).prefetch_related("stages"),
            "attributes": AttributeDefinition.objects.filter(
                organization=organization
            ).order_by("display_order", "name"),
            "webhook_url": request.build_absolute_uri("/webhooks/meta-leads/"),
        },
    )


@crm_login_required
@require_POST
def meta_lead_page_save(request):
    organization = request.crm_user.organization
    page_id = request.POST.get("page_id", "").strip()
    token = request.POST.get("page_access_token", "").strip()
    if not page_id or not token:
        messages.error(request, "Page ID and Page access token are required.")
        return redirect("crm-connect-hub-meta-lead-ad-forms")

    page, _ = MetaLeadPage.objects.get_or_create(
        organization=organization, page_id=page_id
    )
    page.page_name = request.POST.get("page_name", "").strip()
    page.set_page_access_token(token)
    page.set_app_secret(request.POST.get("app_secret", "").strip())
    page.is_active = True
    page.save()
    messages.success(request, "Facebook Page saved.")
    return redirect("crm-connect-hub-meta-lead-ad-forms")


@crm_login_required
@require_POST
def meta_lead_form_save(request):
    organization = request.crm_user.organization
    page = get_object_or_404(
        MetaLeadPage, id=request.POST.get("page"), organization=organization
    )
    pipeline = get_object_or_404(
        Pipeline,
        id=request.POST.get("pipeline"),
        organization=organization,
        is_active=True,
    )
    stage = get_object_or_404(
        Stage, id=request.POST.get("stage"), pipeline=pipeline, is_active=True
    )
    mapping = {
        key: value.strip()
        for key, value in request.POST.items()
        if key in {"name", "phone", "email"} or key.startswith("attribute:")
        if value.strip()
    }
    form, _ = MetaLeadForm.objects.update_or_create(
        page=page,
        form_id=request.POST.get("form_id", "").strip(),
        defaults={
            "form_name": request.POST.get("form_name", "").strip(),
            "pipeline": pipeline,
            "stage": stage,
            "field_mapping": mapping,
            "is_active": True,
        },
    )
    form.full_clean()
    form.save()
    messages.success(request, "Meta lead form routing saved.")
    return redirect("crm-connect-hub-meta-lead-ad-forms")
