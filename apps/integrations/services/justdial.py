from __future__ import annotations

import re
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db.models import F
from django.utils import timezone

from apps.crm.models import AttributeDefinition
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
    ("JustDial Category", "justdial_category"),
    ("JustDial City", "justdial_city"),
    ("JustDial Area", "justdial_area"),
    ("JustDial Branch Area", "justdial_branch_area"),
    ("JustDial Company", "justdial_company"),
    ("JustDial Pincode", "justdial_pincode"),
    ("JustDial Enquiry Date", "justdial_inquiry_date"),
    ("JustDial Enquiry Time", "justdial_inquiry_time"),
    ("JustDial Parent ID", "justdial_parent_id"),
    ("JustDial Product Code", "justdial_product_code"),
    ("JustDial Channel Code", "justdial_channel_code"),
    ("JustDial Agency Code", "justdial_agency_code"),
    ("JustDial State Code", "justdial_state_code"),
    ("JustDial City Code", "justdial_city_code"),
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
    "category": ("category", "categoryname", "category_name", "product", "service"),
    "city": ("city",),
    "area": ("area", "locality"),
    "branch_area": ("brancharea", "branch_area"),
    "company": ("company", "companyname", "company_name"),
    "pincode": ("pincode", "pin", "zipcode", "zip"),
    "date": ("date", "enquirydate", "enquiry_date", "lead_date"),
    "time": ("time", "enquirytime", "enquiry_time", "lead_time"),
    "parent_id": ("parentid", "parent_id"),
    "product_code": ("productcode", "product_code"),
    "channel_code": ("channelcode", "channel_code"),
    "agency_code": ("agencycode", "agency_code"),
    "state_code": ("statecode", "state_code"),
    "city_code": ("citycode", "city_code"),
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

    if len(digits) < 8:
        return ""
    return f"+{digits}"


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
        "justdial_category": _lookup(payload, "category"),
        "justdial_city": _lookup(payload, "city"),
        "justdial_area": _lookup(payload, "area"),
        "justdial_branch_area": _lookup(payload, "branch_area"),
        "justdial_company": _lookup(payload, "company"),
        "justdial_pincode": _lookup(payload, "pincode"),
        "justdial_inquiry_date": _lookup(payload, "date"),
        "justdial_inquiry_time": _lookup(payload, "time"),
        "justdial_parent_id": _lookup(payload, "parent_id"),
        "justdial_product_code": _lookup(payload, "product_code"),
        "justdial_channel_code": _lookup(payload, "channel_code"),
        "justdial_agency_code": _lookup(payload, "agency_code"),
        "justdial_state_code": _lookup(payload, "state_code"),
        "justdial_city_code": _lookup(payload, "city_code"),
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
        "last_error": str(error_message or "")[:500],
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


def process_justdial_lead(
    *,
    integration: JustDialIntegration,
    payload: Any,
    method: str = "GET",
):
    """Create/update one CRM lead from a JustDial push.

    Returns (event, lead, created). Payload validation problems are represented
    as FAILED events and raised as Django ValidationError so the webhook can
    return a non-2xx response instead of silently discarding a lead.
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

    phone = normalize_justdial_phone(_lookup(cleaned, "phone"))
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

    try:
        _ensure_justdial_attribute_definitions(integration.organization)
        try:
            lead, created = upsert_lead(
                organization=integration.organization,
                pipeline=integration.pipeline,
                stage=integration.stage,
                name=name[:150],
                phone=phone,
                email=email,
                attributes=attributes,
                lead_source="justdial",
                # A marketplace lead is not itself an inbound WhatsApp session.
                # Never auto-send the CRM welcome merely because JustDial pushed it.
                send_welcome=False,
            )
        except DuplicateLeadError:
            lead, created = upsert_lead(
                organization=integration.organization,
                pipeline=integration.pipeline,
                stage=integration.stage,
                name=name[:150],
                phone=phone,
                email=email,
                attributes=attributes,
                lead_source="justdial",
                send_welcome=False,
            )
    except ValidationError as exc:
        _record_event(
            integration=integration,
            payload=cleaned,
            method=method,
            status=JustDialLeadEvent.Status.FAILED,
            error_message=str(exc),
        )
        raise

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
