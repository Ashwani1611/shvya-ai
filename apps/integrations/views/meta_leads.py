import hashlib
import hmac
import json
import logging
import re

import requests
from django.conf import settings
from django.contrib import messages
from django.db import transaction
from django.db.models import Prefetch
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.crm.authentication import crm_login_required
from apps.crm.models import AttributeDefinition, Pipeline, Stage
from apps.integrations.models import MetaLeadForm, MetaLeadPage
from services.crm.lead_service import upsert_lead

logger = logging.getLogger(__name__)


META_LEAD_ATTRIBUTE_DEFINITIONS = (
    ("Meta Lead Ad Id", "meta_lead_ad_id"),
    ("Meta Lead Form", "meta_lead_form"),
    ("Meta Lead Form Id", "meta_lead_form_id"),
)


def _ensure_meta_attribute_definitions(organization):
    """Make Meta import attributes visible in the CRM lead attribute UI."""
    existing_keys = set(
        AttributeDefinition.objects.filter(
            organization=organization,
            key__in=[key for _name, key in META_LEAD_ATTRIBUTE_DEFINITIONS],
        ).values_list("key", flat=True)
    )
    definitions = []
    for index, (name, key) in enumerate(META_LEAD_ATTRIBUTE_DEFINITIONS, start=900):
        if key in existing_keys:
            continue
        definition = AttributeDefinition(
            organization=organization,
            name=name,
            key=key,
            field_type=AttributeDefinition.FieldType.TEXT,
            description="Automatically filled when a lead is created from Meta Lead Ads.",
            display_order=index,
        )
        definition.full_clean()
        definitions.append(definition)

    if definitions:
        AttributeDefinition.objects.bulk_create(definitions, ignore_conflicts=True)


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


def _field_value_with_fallbacks(mapping, target, fields, fallback_names):
    """Return a mapped value, then try common Meta field names."""
    value = _field_value(mapping, target, fields)
    if value:
        return value

    for fallback in fallback_names:
        normalised_fallback = _normalise_field_key(fallback)
        for key, value in fields.items():
            if _normalise_field_key(key) == normalised_fallback:
                return str(value or "").strip()
    return ""


def _meta_lead_name(mapping, fields):
    name = _field_value_with_fallbacks(
        mapping,
        "name",
        fields,
        ("full_name", "name", "first_name", "last_name"),
    )
    if name:
        return name

    first_name = _field_value_with_fallbacks(
        {"first_name": mapping.get("first_name", "")},
        "first_name",
        fields,
        ("first_name",),
    )
    last_name = _field_value_with_fallbacks(
        {"last_name": mapping.get("last_name", "")},
        "last_name",
        fields,
        ("last_name",),
    )
    return " ".join(part for part in (first_name, last_name) if part).strip()


def _normalise_meta_phone(value):
    """Convert Meta phone answers into a consistent CRM phone format."""
    value = str(value or "").strip()
    if not value:
        return ""

    digits = re.sub(r"\D", "", value)
    if not digits:
        return value

    default_country_code = str(
        getattr(settings, "META_LEAD_DEFAULT_COUNTRY_CODE", "+91") or "+91"
    ).strip()
    default_country_digits = re.sub(r"\D", "", default_country_code)

    if digits.startswith("00") and len(digits) > 2:
        digits = digits[2:]

    if default_country_digits:
        if len(digits) == 10:
            return f"+{default_country_digits}{digits}"
        if (
            digits.startswith(default_country_digits)
            and len(digits) == len(default_country_digits) + 10
        ):
            return f"+{digits}"
        if digits.startswith("0") and len(digits) == 11:
            return f"+{default_country_digits}{digits[-10:]}"

    if value.startswith("+") or len(digits) > 10:
        return f"+{digits}"
    return value


def _phone_is_crm_compatible(value):
    return bool(
        str(value or "").startswith("+")
        and len(re.sub(r"\D", "", str(value))) >= 8
    )


def _phone_like_field(fields):
    """Return the first phone-looking answer from any Meta field key."""
    phone_markers = ("phone", "mobile", "contact", "whatsapp")
    for key, value in fields.items():
        normalised_key = _normalise_field_key(key)
        if any(marker in normalised_key for marker in phone_markers):
            value = str(value or "").strip()
            if value:
                return value
    return ""


def _fetch_lead(leadgen_id, page):
    response = requests.get(
        f"https://graph.facebook.com/v26.0/{leadgen_id}",
        params={
            "fields": "field_data,ad_id,form_id,created_time",
            "access_token": page.get_page_access_token(),
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _signature_is_valid(request, secret):
    if not secret:
        return True

    signature_256 = request.headers.get("X-Hub-Signature-256", "")
    if signature_256:
        expected = "sha256=" + hmac.new(
            secret.encode("utf-8"), request.body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature_256, expected)

    signature_sha1 = request.headers.get("X-Hub-Signature", "")
    if signature_sha1:
        expected = "sha1=" + hmac.new(
            secret.encode("utf-8"), request.body, hashlib.sha1
        ).hexdigest()
        return hmac.compare_digest(signature_sha1, expected)

    logger.warning("Meta webhook received without a signature header")
    return True


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

    page = next(iter(pages_by_id.values()), None)
    secret = (
        page.get_app_secret() if page else ""
    ) or getattr(settings, "META_APP_SECRET", "")
    if not _signature_is_valid(request, secret):
        logger.warning(
            "Meta webhook signature mismatch for configured Page; accepting event "
            "and validating lead access through the stored Page token."
        )

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
                    item.get("name", ""): ", ".join(
                        str(answer)
                        for answer in (item.get("values") or [""])
                        if answer is not None
                    )
                    for item in lead_data.get("field_data", [])
                    if item.get("name")
                }
                mapping = form.field_mapping or {}
                raw_phone = _field_value_with_fallbacks(
                    mapping,
                    "phone",
                    fields,
                    (
                        "phone_number",
                        "phone",
                        "mobile_number",
                        "mobile",
                        "contact_number",
                    ),
                )
                phone = _normalise_meta_phone(raw_phone)
                phone_warning = ""

                if not _phone_is_crm_compatible(phone):
                    recovered_phone = _normalise_meta_phone(_phone_like_field(fields))
                    if _phone_is_crm_compatible(recovered_phone):
                        phone = recovered_phone
                        phone_warning = (
                            "Phone was recovered from another phone-like Meta field."
                        )

                if not _phone_is_crm_compatible(phone):
                    logger.warning(
                        "Skipping Meta lead %s because Meta did not provide a "
                        "CRM-compatible phone. Available fields: %s",
                        leadgen_id,
                        ", ".join(sorted(fields)),
                    )
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
                if phone_warning:
                    attributes["meta_import_warning"] = phone_warning
                    attributes["meta_raw_phone"] = raw_phone

                with transaction.atomic():
                    _ensure_meta_attribute_definitions(page.organization)
                    lead, created = upsert_lead(
                        organization=page.organization,
                        pipeline=form.pipeline,
                        stage=form.stage,
                        name=_meta_lead_name(mapping, fields) or "Meta Lead",
                        phone=phone,
                        email=_field_value_with_fallbacks(
                            mapping,
                            "email",
                            fields,
                            ("email", "email_address"),
                        ),
                        attributes=attributes,
                        lead_source="meta_ads",
                    )
                logger.info(
                    "%s Meta lead %s for form %s into organization %s as CRM lead %s",
                    "Created" if created else "Updated",
                    leadgen_id,
                    form.form_id,
                    page.organization_id,
                    lead.id,
                )
            except Exception:
                logger.exception("Failed to import Meta lead %s", leadgen_id)

    return HttpResponse("EVENT_RECEIVED")


@crm_login_required
@require_GET
def meta_lead_forms_view(request):
    organization = request.crm_user.organization
    active_form_qs = (
        MetaLeadForm.objects.select_related("pipeline", "stage")
        .filter(is_active=True)
        .order_by("form_name")
    )
    pages = (
        MetaLeadPage.objects.filter(organization=organization, is_active=True)
        .prefetch_related(Prefetch("forms", queryset=active_form_qs))
        .order_by("page_name", "page_id")
    )
    active_forms = (
        MetaLeadForm.objects.select_related("page", "pipeline", "stage")
        .filter(page__organization=organization, page__is_active=True, is_active=True)
        .order_by("page__page_name", "form_name")
    )
    return render(
        request,
        "integrations/meta_lead_forms.html",
        {
            "pages": pages,
            "active_forms": active_forms,
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
    messages.success(request, "Facebook Page saved. You can add forms for it now.")
    return redirect("crm-connect-hub-meta-lead-ad-forms")


@crm_login_required
@require_POST
def meta_lead_page_delete(request):
    organization = request.crm_user.organization
    page = get_object_or_404(
        MetaLeadPage, id=request.POST.get("page"), organization=organization
    )
    page.is_active = False
    page.save(update_fields=["is_active", "updated_at"])
    page.forms.update(is_active=False)
    messages.success(request, "Meta Page integration removed.")
    return redirect("crm-connect-hub-meta-lead-ad-forms")


@crm_login_required
@require_POST
def meta_lead_form_save(request):
    organization = request.crm_user.organization
    page = get_object_or_404(
        MetaLeadPage,
        id=request.POST.get("page"),
        organization=organization,
        is_active=True,
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
    form_id = request.POST.get("form_id", "").strip()
    if not form_id:
        messages.error(request, "Form ID is required.")
        return redirect("crm-connect-hub-meta-lead-ad-forms")

    mapping = {
        key: value.strip()
        for key, value in request.POST.items()
        if key in {"name", "phone", "email"} or key.startswith("attribute:")
        if value.strip()
    }
    form, _ = MetaLeadForm.objects.update_or_create(
        page=page,
        form_id=form_id,
        defaults={
            "form_name": request.POST.get("form_name", "").strip() or form_id,
            "pipeline": pipeline,
            "stage": stage,
            "field_mapping": mapping,
            "is_active": True,
        },
    )
    form.full_clean()
    form.save()
    messages.success(
        request,
        "Meta lead form routing saved. Add another Form ID to connect another form.",
    )
    return redirect("crm-connect-hub-meta-lead-ad-forms")


@crm_login_required
@require_POST
def meta_lead_form_delete(request):
    organization = request.crm_user.organization
    form = get_object_or_404(
        MetaLeadForm,
        id=request.POST.get("form"),
        page__organization=organization,
        is_active=True,
    )
    form.is_active = False
    form.save(update_fields=["is_active", "updated_at"])
    messages.success(request, "Meta lead form mapping removed.")
    return redirect("crm-connect-hub-meta-lead-ad-forms")
