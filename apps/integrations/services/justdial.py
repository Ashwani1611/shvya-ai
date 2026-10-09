from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.crm.models import AttributeDefinition, Lead
from apps.integrations.justdial_models import JustDialIntegration, JustDialLeadEvent
from services.crm.lead_service import DuplicateLeadError, upsert_lead


SENSITIVE_PAYLOAD_KEYS = {
    "token",
    "accesstoken",
    "accesskey",
    "apikey",
    "clientkey",
    "secret",
    "clientsecret",
    "password",
    "passwd",
    "authorization",
    "username",
    "userid",
}


JUSTDIAL_ATTRIBUTE_DEFINITIONS = (
    ("JustDial Lead ID", "justdial_lead_id"),
    ("JustDial Lead Type", "justdial_lead_type"),
    ("JustDial Prefix", "justdial_prefix"),
    ("JustDial Category", "justdial_category"),
    ("JustDial City", "justdial_city"),
    ("JustDial State", "justdial_state"),
    ("JustDial Area", "justdial_area"),
    ("JustDial Branch Area", "justdial_branch_area"),
    ("JustDial Company", "justdial_company"),
    ("JustDial Pincode", "justdial_pincode"),
    ("JustDial Enquiry Date", "justdial_inquiry_date"),
    ("JustDial Enquiry Time", "justdial_inquiry_time"),
    ("JustDial Parent ID", "justdial_parent_id"),
    ("JustDial Branch Pin", "justdial_branch_pin"),
    ("JustDial DNC Mobile", "justdial_dnc_mobile"),
    ("JustDial DNC Phone", "justdial_dnc_phone"),
)

FIELD_ALIASES = {
    "lead_id": ("leadid", "lead_id", "enquiryid", "enquiry_id", "id"),
    "name": ("name", "customername", "customer_name", "fullname", "full_name"),
    "phone": (
        "mobile",
        "mobileno",
        "mobile_no",
        "mobile_number",
        "phone",
        "phoneno",
        "phone_no",
        "contact",
        "contact_number",
    ),
    "email": ("email", "emailid", "email_id", "emailaddress", "email_address"),
    "lead_type": ("leadtype", "lead_type", "enquirytype", "enquiry_type"),
    "prefix": ("prefix",),
    "category": ("category", "categoryname", "category_name", "product", "service"),
    "city": ("city",),
    "state": ("state", "statename", "state_name"),
    "area": ("area", "locality"),
    "branch_area": ("brancharea", "branch_area"),
    "company": ("company", "companyname", "company_name"),
    "pincode": ("pincode", "pin", "zipcode", "zip"),
    "date": ("date", "enquirydate", "enquiry_date", "lead_date"),
    "time": ("time", "enquirytime", "enquiry_time", "lead_time"),
    "parent_id": ("parentid", "parent_id"),
    "branch_pin": ("branchpin", "branch_pin"),
    "dnc_mobile": ("dncmobile", "dnc_mobile"),
    "dnc_phone": ("dncphone", "dnc_phone"),
}


def _normalise_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").strip().casefold())


def _string_value(value: Any) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if value is None:
        return ""
    if isinstance(value, (dict, tuple, set)):
        return ""
    return str(value).strip()


def clean_payload(payload: Any) -> dict[str, Any]:
    """Return a JSON-safe, bounded dictionary for ingestion and audit logging."""
    if not isinstance(payload, dict):
        return {}

    cleaned: dict[str, Any] = {}
    for raw_key, raw_value in list(payload.items())[:100]:
        key = str(raw_key or "").strip()[:120]
        if not key:
            continue
        if _normalise_key(key) in SENSITIVE_PAYLOAD_KEYS:
            cleaned[key] = "[REDACTED]"
        elif isinstance(raw_value, list):
            values = [_string_value(item)[:2000] for item in raw_value[:10]]
            cleaned[key] = values
        elif isinstance(raw_value, dict):
            cleaned[key] = {
                str(k)[:120]: _string_value(v)[:2000]
                for k, v in list(raw_value.items())[:50]
            }
        else:
            cleaned[key] = _string_value(raw_value)[:4000]
    return cleaned


def _lookup(payload: dict[str, Any], canonical_field: str) -> str:
    aliases = FIELD_ALIASES[canonical_field]
    indexed = {
        _normalise_key(key): _string_value(value)
        for key, value in payload.items()
    }
    for alias in aliases:
        value = indexed.get(_normalise_key(alias), "")
        if value:
            return value
    return ""


def normalize_justdial_phone(value: Any, *, default_country_code: str = "91") -> str:
    """Normalize common JustDial India phone formats to SHVYA +country format."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00") and len(digits) > 2:
        digits = digits[2:]

    country_digits = re.sub(r"\D", "", default_country_code) or "91"
    if len(digits) == 10:
        digits = f"{country_digits}{digits}"
    elif len(digits) == 11 and digits.startswith("0"):
        digits = f"{country_digits}{digits[-10:]}"

    # E.164 supports at most 15 digits; reject placeholder and malformed numbers.
    if not 8 <= len(digits) <= 15:
        return ""
    return f"+{digits}"


def _normalized_inbound_phone(payload: dict[str, Any]) -> str:
    """Try mobile first, then the other phone aliases if a value is unusable."""
    values = {_normalise_key(key): _string_value(value) for key, value in payload.items()}
    for alias in FIELD_ALIASES["phone"]:
        phone = normalize_justdial_phone(values.get(_normalise_key(alias), ""))
        if phone:
            return phone
    return ""


def normalize_justdial_email(value: Any) -> str:
    """Keep a malformed marketplace email from dropping an otherwise valid lead."""
    email = _string_value(value)[:254]
    if not email:
        return ""
    try:
        validate_email(email)
    except ValidationError:
        return ""
    return email


def _ensure_justdial_attribute_definitions(organization) -> None:
    keys = [key for _name, key in JUSTDIAL_ATTRIBUTE_DEFINITIONS]
    existing = set(
        AttributeDefinition.objects.filter(
            organization=organization,
            key__in=keys,
        ).values_list("key", flat=True)
    )
    pending = []
    for index, (name, key) in enumerate(JUSTDIAL_ATTRIBUTE_DEFINITIONS, start=940):
        if key in existing:
            continue
        definition = AttributeDefinition(
            organization=organization,
            name=name,
            key=key,
            field_type=AttributeDefinition.FieldType.TEXT,
            description="Automatically filled from a JustDial lead-push event.",
            display_order=index,
        )
        pending.append(definition)
    if pending:
        AttributeDefinition.objects.bulk_create(pending, ignore_conflicts=True)


def _lead_attributes(payload: dict[str, Any]) -> dict[str, str]:
    mapping = {
        "justdial_lead_id": _lookup(payload, "lead_id"),
        "justdial_lead_type": _lookup(payload, "lead_type"),
        "justdial_prefix": _lookup(payload, "prefix"),
        "justdial_category": _lookup(payload, "category"),
        "justdial_city": _lookup(payload, "city"),
        "justdial_state": _lookup(payload, "state"),
        "justdial_area": _lookup(payload, "area"),
        "justdial_branch_area": _lookup(payload, "branch_area"),
        "justdial_company": _lookup(payload, "company"),
        "justdial_pincode": _lookup(payload, "pincode"),
        "justdial_inquiry_date": _lookup(payload, "date"),
        "justdial_inquiry_time": _lookup(payload, "time"),
        "justdial_parent_id": _lookup(payload, "parent_id"),
        "justdial_branch_pin": _lookup(payload, "branch_pin"),
        "justdial_dnc_mobile": _lookup(payload, "dnc_mobile"),
        "justdial_dnc_phone": _lookup(payload, "dnc_phone"),
    }
    return {key: value for key, value in mapping.items() if value}


def _record_event(
    *,
    integration: JustDialIntegration,
    payload: dict[str, Any],
    method: str,
    status: str,
    lead=None,
    error_message: str = "",
) -> JustDialLeadEvent:
    event = JustDialLeadEvent.objects.create(
        integration=integration,
        organization=integration.organization,
        lead=lead,
        external_lead_id=_lookup(payload, "lead_id")[:160],
        method=(method or "GET")[:8],
        status=status,
        payload=payload,
        error_message=str(error_message or "")[:500],
    )

    updates = {
        "received_count": F("received_count") + 1,
        "last_received_at": timezone.now(),
        "last_error": (str(error_message or "")[:500] if status == JustDialLeadEvent.Status.FAILED else ""),
    }
    if status == JustDialLeadEvent.Status.CREATED:
        updates["created_count"] = F("created_count") + 1
    elif status == JustDialLeadEvent.Status.UPDATED:
        updates["updated_count"] = F("updated_count") + 1
    elif status == JustDialLeadEvent.Status.IGNORED:
        updates["ignored_count"] = F("ignored_count") + 1
    elif status == JustDialLeadEvent.Status.FAILED:
        updates["error_count"] = F("error_count") + 1

    JustDialIntegration.objects.filter(pk=integration.pk).update(**updates)
    return event


def record_justdial_failure(
    *,
    integration: JustDialIntegration,
    payload: Any,
    method: str,
    error_message: str,
) -> JustDialLeadEvent:
    """Persist an unexpected webhook failure without exposing internals."""
    return _record_event(
        integration=integration,
        payload=clean_payload(payload),
        method=method,
        status=JustDialLeadEvent.Status.FAILED,
        error_message=error_message,
    )


class JustDialRoutingError(Exception):
    """The configured organization/pipeline/stage cannot accept leads."""


def _validate_routing(integration: JustDialIntegration) -> None:
    if (
        not integration.is_provisioned
        or not integration.organization.is_active
        or not integration.pipeline.is_active
        or not integration.stage.is_active
        or integration.pipeline.organization_id != integration.organization_id
        or integration.stage.pipeline_id != integration.pipeline_id
    ):
        raise JustDialRoutingError(
            "JustDial destination is not an active stage in this organization."
        )


def process_justdial_lead(
    *,
    integration: JustDialIntegration,
    payload: Any,
    method: str = "POST",
):
    """Ingest one JustDial push and return (event, lead, created).

    Successful lead IDs are durable idempotency keys: the event record acts as
    the receipt, including after a lead is deleted. A row lock on the owning
    integration serializes concurrent pushes for the same organization.
    """
    cleaned = clean_payload(payload)
    if not integration.is_enabled or not integration.is_provisioned:
        event = _record_event(
            integration=integration,
            payload=cleaned,
            method=method,
            status=JustDialLeadEvent.Status.IGNORED,
            error_message="JustDial integration is not enabled.",
        )
        return event, None, False

    phone = _normalized_inbound_phone(cleaned)
    if not phone:
        message = "JustDial payload did not contain a CRM-compatible phone number."
        _record_event(
            integration=integration,
            payload=cleaned,
            method=method,
            status=JustDialLeadEvent.Status.FAILED,
            error_message=message,
        )
        raise ValidationError(message)

    name = _lookup(cleaned, "name") or "JustDial Lead"
    email = normalize_justdial_email(_lookup(cleaned, "email"))
    attributes = _lead_attributes(cleaned)
    external_id = _lookup(cleaned, "lead_id")[:160]

    try:
        with transaction.atomic():
            # Ensure rotation, disable and replay checks operate on a single,
            # current integration configuration rather than a stale snapshot.
            integration = (
                JustDialIntegration.objects.select_for_update(of=("self",))
                .select_related("organization", "pipeline", "stage")
                .get(pk=integration.pk)
            )
            if not integration.is_enabled or not integration.is_provisioned:
                event = _record_event(
                    integration=integration,
                    payload=cleaned,
                    method=method,
                    status=JustDialLeadEvent.Status.IGNORED,
                    error_message="JustDial integration is not enabled.",
                )
                return event, None, False
            _validate_routing(integration)

            if external_id:
                receipt = (
                    JustDialLeadEvent.objects.filter(
                        integration=integration,
                        external_lead_id=external_id,
                        status__in=(
                            JustDialLeadEvent.Status.CREATED,
                            JustDialLeadEvent.Status.UPDATED,
                        ),
                    )
                    .select_related("lead")
                    .first()
                )
                if receipt is not None:
                    event = _record_event(
                        integration=integration,
                        payload=cleaned,
                        method=method,
                        status=JustDialLeadEvent.Status.IGNORED,
                        lead=receipt.lead,
                        error_message="Duplicate JustDial lead ID.",
                    )
                    return event, receipt.lead, False

            _ensure_justdial_attribute_definitions(integration.organization)
            existing = (
                Lead.objects.select_for_update()
                .filter(organization=integration.organization, phone=phone)
                .first()
            )

            # New enquiries for an existing contact must not reset a Qualified
            # or human-managed lead to the configured incoming stage.
            # Also preserve names/emails originating from another source.
            destination_pipeline = None if existing else integration.pipeline
            destination_stage = None if existing else integration.stage
            safe_name = (
                existing.name
                if existing and existing.lead_source != "justdial"
                else name if name != "JustDial Lead" or not existing else existing.name
            )
            safe_email = (
                existing.email
                if existing and existing.lead_source != "justdial" and existing.email
                else email
            )
            try:
                lead, created = upsert_lead(
                    organization=integration.organization,
                    pipeline=destination_pipeline,
                    stage=destination_stage,
                    name=safe_name[:150],
                    phone=phone,
                    email=safe_email,
                    attributes=attributes,
                    lead_source="justdial",
                    send_welcome=False,
                )
            except DuplicateLeadError:
                # A different lead source may have created this phone between
                # our lookup and the upsert; retry without changing its route.
                existing = Lead.objects.filter(
                    organization=integration.organization,
                    phone=phone,
                ).first()
                lead, created = upsert_lead(
                    organization=integration.organization,
                    pipeline=None if existing else integration.pipeline,
                    stage=None if existing else integration.stage,
                    name=(
                        existing.name
                        if existing and existing.lead_source != "justdial"
                        else name
                    )[:150],
                    phone=phone,
                    email=(
                        existing.email
                        if existing and existing.lead_source != "justdial" and existing.email
                        else email
                    ),
                    attributes=attributes,
                    lead_source="justdial",
                    send_welcome=False,
                )

            status = (
                JustDialLeadEvent.Status.CREATED
                if created
                else JustDialLeadEvent.Status.UPDATED
            )
            event = _record_event(
                integration=integration,
                payload=cleaned,
                method=method,
                status=status,
                lead=lead,
            )
            return event, lead, created
    except (ValidationError, JustDialRoutingError) as exc:
        # Record after the atomic block rolls back; otherwise even the failure
        # event would disappear with the unsuccessful CRM transaction.
        record_justdial_failure(
            integration=integration,
            payload=cleaned,
            method=method,
            error_message=str(exc),
        )
        raise
