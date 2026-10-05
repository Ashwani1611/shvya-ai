"""Conversions API configuration and CRM event capture; no provider I/O here."""

import hashlib
import ipaddress
import logging
import re
import uuid
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from apps.crm.models import Lead, Pipeline, Stage
from apps.integrations.meta_conversions_forms import (
    ACTION_SOURCES, CURRENCIES, DEFAULT_USER_DATA_FIELDS, USER_DATA_FIELDS,
    MetaConversionMappingForm, MetaConversionsSettingsForm, shareable_attributes,
)
from apps.integrations.models import (
    MetaConversionDelivery as Delivery, MetaConversionMapping as Mapping,
    MetaConversionsConfiguration as Configuration,
)

logger = logging.getLogger(__name__)


def ensure_default_mappings(configuration):
    for stage in Stage.objects.filter(
        pipeline__organization=configuration.organization, pipeline__is_active=True,
        is_active=True, name__iregex=r"^new leads?$",
    ).select_related("pipeline"):
        Mapping.objects.get_or_create(
            configuration=configuration, stage=stage,
            defaults={"pipeline": stage.pipeline, "event_name": "Lead", "is_default": True},
        )


def get_configuration(organization):
    configuration, _ = Configuration.objects.get_or_create(
        organization=organization, defaults={"user_data_fields": list(DEFAULT_USER_DATA_FIELDS)},
    )
    ensure_default_mappings(configuration)
    return configuration


def _row(model, **filters):
    try:
        row = model.objects.filter(**filters).first()
    except (ValidationError, ValueError, TypeError):
        row = None
    if row is None:
        raise ValidationError("This item is unavailable in your organization.")
    return row


@transaction.atomic
def save_settings(configuration, data):
    configuration = Configuration.objects.select_for_update().get(pk=configuration.pk)
    form = MetaConversionsSettingsForm(data, configuration=configuration)
    if not form.is_valid():
        raise ValidationError(dict(form.errors))
    values = form.cleaned_data.copy()
    token = values.pop("access_token")
    if values["dataset_id"] != configuration.dataset_id:
        configuration.destination_version = uuid.uuid4()
        configuration.verified_at = None
    if token:
        configuration.set_access_token(token)
        configuration.verified_at = None
    for key, value in values.items():
        setattr(configuration, key, value)
    configuration.save()
    return configuration


@transaction.atomic
def save_mapping(configuration, data):
    Configuration.objects.select_for_update().get(pk=configuration.pk)
    data = dict(data)
    if data.get("value_source") != "static":
        data["static_value"] = ""
    if data.get("value_source") != "attribute":
        data["value_attribute"] = ""
    mapping = (_row(Mapping, configuration=configuration, pk=data["id"])
               if data.get("id") else Mapping(configuration=configuration))
    form = MetaConversionMappingForm(data, instance=mapping, configuration=configuration)
    if not form.is_valid():
        raise ValidationError(dict(form.errors))
    mapping = form.save(commit=False)
    if Mapping.objects.filter(configuration=configuration, stage=mapping.stage).exclude(pk=mapping.pk).exists():
        raise ValidationError({"stage": "This stage already has an event mapping. Edit that mapping instead."})
    mapping.full_clean()
    mapping.save()
    return mapping


def _hashed(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def build_user_data(configuration, lead):
    selected = set(configuration.user_data_fields)
    attributes = lead.attributes or {}
    result = {}
    if "email" in selected and lead.email:
        result["em"] = [_hashed(lead.email.strip().lower())]
    phone = re.sub(r"\D", "", lead.phone or "").lstrip("0")
    if "phone" in selected and phone and (lead.phone or "").startswith("+"):
        result["ph"] = [_hashed(phone)]
    if "name" in selected and lead.name and lead.name.lower() not in {"meta lead", "new lead", "instagram lead"}:
        parts = lead.name.strip().split(maxsplit=1)
        for key, value in zip(("fn", "ln"), parts):
            normalized = "".join(c for c in value.lower() if c.isalpha())
            if normalized:
                result[key] = [_hashed(normalized)]
    if "external_id" in selected:
        result["external_id"] = [_hashed(str(lead.pk))]
    if "lead_id" in selected:
        meta_id = str(attributes.get("meta_leadgen_id") or "").strip()
        if meta_id:
            if not re.fullmatch(r"[0-9]{15,20}", meta_id):
                raise ValidationError("The stored Meta Lead ID is invalid. Check the Lead Ads import.")
            result["lead_id"] = meta_id
    for source, target in (("city", "ct"), ("state", "st"), ("zip_code", "zp"), ("country", "country")):
        if source not in selected:
            continue
        value = str(attributes.get(source) or "").strip().lower()
        if source in {"city", "state"}:
            value = "".join(c for c in value if c.isalpha())
        elif source == "zip_code":
            value = re.sub(r"[\s-]", "", value)
            if str(attributes.get("country") or "").lower() == "us":
                value = value[:5]
        elif not re.fullmatch(r"[a-z]{2}", value):
            value = ""
        if value:
            result[target] = [_hashed(value)]
    gender = {"male": "m", "female": "f", "m": "m", "f": "f"}.get(str(attributes.get("gender") or "").lower())
    if "gender" in selected and gender:
        result["ge"] = [_hashed(gender)]
    if "date_of_birth" in selected and attributes.get("date_of_birth"):
        try:
            birthday = date.fromisoformat(str(attributes["date_of_birth"]))
            if date(1900, 1, 1) <= birthday <= timezone.localdate():
                result["db"] = [_hashed(birthday.strftime("%Y%m%d"))]
        except ValueError:
            pass
    if "ip_address" in selected:
        try:
            result["client_ip_address"] = str(ipaddress.ip_address(str(attributes.get("ip_address") or "")))
        except ValueError:
            pass
    for source, target in (("user_agent", "client_user_agent"), ("wa_ref_ctwa_clid", "ctwa_clid")):
        value = attributes.get(source)
        if source in selected and isinstance(value, str) and value.strip():
            result[target] = value.strip()[:2000]
    for key in ("fbp", "fbc"):
        value = str(attributes.get(key) or "")
        if key in selected and re.fullmatch(r"fb\.[0-9]+\.[0-9]{10,16}\.[^\s]{1,1000}", value):
            result[key] = value
    if not result:
        raise ValidationError("No selected customer identifiers are available on this lead.")
    return result


def build_event(configuration, mapping, lead, *, event_id, occurred_at):
    if lead.organization_id != configuration.organization_id or mapping.configuration_id != configuration.pk:
        raise ValidationError("The event does not belong to this organization.")
    if mapping.pipeline.organization_id != lead.organization_id or mapping.stage.pipeline_id != mapping.pipeline_id:
        raise ValidationError("The event mapping has an invalid pipeline or stage.")
    event = {
        "event_name": mapping.event_name, "event_time": int(occurred_at.timestamp()),
        "event_id": event_id, "action_source": mapping.action_source,
        "user_data": build_user_data(configuration, lead),
    }
    custom_data = {}
    if lead.lead_source == "meta_ads":
        event["action_source"] = "system_generated"
        custom_data.update(event_source="crm", lead_event_source="SHVYA AI")
    if event["action_source"] == "website":
        url = str((lead.attributes or {}).get("event_source_url") or "")
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise ValidationError("The customer's event page URL is invalid.") from None
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or len(url) > 2048 or not event["user_data"].get("client_user_agent")):
            raise ValidationError("Website events need the customer's page URL and browser in lead attributes.")
        event["event_source_url"] = url[:2048]
    if mapping.value_source != "none":
        raw_value = (mapping.static_value if mapping.value_source == "static"
                     else (lead.attributes or {}).get(mapping.value_attribute))
        try:
            value = Decimal(str(raw_value))
            if not value.is_finite() or value < 0 or value >= Decimal("1e14"):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise ValidationError("The mapped event value is missing or is not a valid non-negative number.") from None
        if mapping.currency not in CURRENCIES:
            raise ValidationError("The mapped event value needs a supported currency.")
        custom_data.update(value=float(value), currency=mapping.currency)
    allowed_keys = {a.key for a in shareable_attributes(configuration.organization)}
    for key in configuration.custom_attribute_keys:
        value = (lead.attributes or {}).get(key)
        if key in allowed_keys and isinstance(value, (str, int, float, bool)):
            if isinstance(value, float) and (value != value or abs(value) == float("inf")):
                continue
            custom_data[key] = value[:500] if isinstance(value, str) else value
    if custom_data:
        event["custom_data"] = custom_data
    return event


def enqueue_delivery(delivery):
    def publish():
        from apps.integrations.tasks import deliver_meta_conversion_task
        try:
            deliver_meta_conversion_task.delay(str(delivery.pk))
            Delivery.objects.filter(pk=delivery.pk).update(last_enqueued_at=timezone.now())
        except Exception:
            # Beat recovers the durable row. Broker availability cannot undo a CRM save.
            logger.warning("Meta event publication deferred delivery=%s", delivery.pk)
    transaction.on_commit(publish)


def queue_event(configuration, mapping, lead, *, occurred_at, is_test=False, is_probe=False, event_id=None):
    if configuration.event_scope == "meta_leads" and lead.lead_source != "meta_ads":
        raise ValidationError("Choose a lead imported from Meta Lead Ads for this connection.")
    if not configuration.dataset_id or not configuration.has_access_token:
        raise ValidationError("Save the dataset ID and access token first.")
    if is_test and not configuration.test_event_code:
        raise ValidationError("Save the Test Events code from Meta Events Manager first.")
    event_id = event_id or str(uuid.uuid4())
    error, payload = "", {"event_name": mapping.event_name, "event_time": int(occurred_at.timestamp()), "event_id": event_id}
    try:
        payload = build_event(configuration, mapping, lead, event_id=event_id, occurred_at=occurred_at)
    except ValidationError as exc:
        error = "; ".join(exc.messages)[:500]
    delivery, created = Delivery.objects.get_or_create(
        configuration=configuration, event_id=event_id,
        defaults={
            "organization_id": configuration.organization_id, "mapping": mapping, "lead": lead,
            "dataset_id": configuration.dataset_id, "destination_version": configuration.destination_version,
            "payload": payload, "is_test": is_test,
            "is_probe": is_probe,
            "test_event_code": configuration.test_event_code if is_test else "",
            "status": Delivery.Status.FAILED if error else Delivery.Status.QUEUED,
            "error_message": error,
        },
    )
    if created and not error:
        enqueue_delivery(delivery)
    return delivery


def capture_stage_event(configuration, lead, *, occurred_at=None):
    if not configuration.is_enabled:
        return
    if configuration.event_scope == "meta_leads" and lead.lead_source != "meta_ads":
        return
    if lead.pipeline.organization_id != configuration.organization_id or lead.stage.pipeline_id != lead.pipeline_id:
        return
    if lead.stage.name.strip().casefold() in {"new lead", "new leads"}:
        Mapping.objects.get_or_create(
            configuration=configuration, stage=lead.stage,
            defaults={"pipeline": lead.pipeline, "is_default": True, "event_name": "Lead"},
        )
    mapping = Mapping.objects.select_related("pipeline", "stage").filter(
        configuration=configuration, pipeline_id=lead.pipeline_id, stage_id=lead.stage_id,
        is_enabled=True, pipeline__is_active=True, stage__is_active=True,
    ).first()
    if mapping is None:
        return
    occurred_at = occurred_at or lead.updated_at or timezone.now()
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"shvya-meta:{configuration.pk}:{lead.pk}:{mapping.pk}:{occurred_at.isoformat()}"))
    queue_event(configuration, mapping, lead, occurred_at=occurred_at,
                event_id=event_id, is_test=configuration.test_mode)


@transaction.atomic
def perform_action(configuration, data):
    configuration = Configuration.objects.select_for_update().get(
        pk=configuration.pk, organization_id=configuration.organization_id,
    )
    action = data.get("action")
    if action == "save_settings":
        save_settings(configuration, data)
        return "Connection settings saved."
    if action == "save_mapping":
        save_mapping(configuration, data)
        return "Event mapping saved."
    if action == "delete_mapping":
        mapping = _row(Mapping, configuration=configuration, pk=data.get("id"))
        if mapping.is_default:
            raise ValidationError("The default New Lead mapping can be paused but cannot be deleted.")
        mapping.deliveries.filter(status__in=[Delivery.Status.QUEUED, Delivery.Status.RETRYING]).update(
            status=Delivery.Status.SKIPPED, error_message="The event mapping was removed before delivery.",
        )
        mapping.delete()
        return "Event mapping removed."
    if action == "disconnect":
        configuration.is_enabled = False
        configuration.set_access_token("")
        configuration.verified_at = None
        configuration.destination_version = uuid.uuid4()
        configuration.save()
        return "Connection disconnected."
    if action == "test_event":
        mapping = _row(Mapping, configuration=configuration, pk=data.get("mapping_id"))
        lead = _row(Lead, organization_id=configuration.organization_id, pk=data.get("lead_id"))
        if lead.pipeline_id != mapping.pipeline_id or lead.stage_id != mapping.stage_id:
            raise ValidationError("Choose a lead currently in the mapped pipeline and stage.")
        delivery = queue_event(configuration, mapping, lead, occurred_at=timezone.now(), is_test=True, is_probe=True)
        if delivery.status == Delivery.Status.FAILED:
            raise ValidationError(delivery.error_message)
        return "Test event queued. Check its delivery status and Meta Events Manager."
    if action == "retry_delivery":
        delivery = _row(Delivery, configuration=configuration, organization_id=configuration.organization_id, pk=data.get("id"))
        if delivery.status != Delivery.Status.FAILED:
            raise ValidationError("Only failed deliveries can be retried.")
        if not delivery.payload.get("user_data"):
            raise ValidationError("Fix the missing lead data, then send a new test or record a new stage change.")
        if delivery.destination_version != configuration.destination_version:
            raise ValidationError("This event belongs to a previous dataset connection.")
        if not configuration.is_enabled and not delivery.is_probe:
            raise ValidationError("Enable event tracking before retrying a live event.")
        if delivery.mapping_id is None:
            raise ValidationError("This event mapping was removed. Create a mapping and record a new stage change.")
        if not delivery.is_probe and not delivery.mapping.is_enabled:
            raise ValidationError("Enable this event mapping before retrying its delivery.")
        if delivery.created_at < timezone.now() - timedelta(days=7):
            raise ValidationError("This event is outside Meta's seven-day delivery window.")
        delivery.status, delivery.attempt_count = Delivery.Status.QUEUED, 0
        delivery.next_attempt_at, delivery.lease_until, delivery.lease_token = timezone.now(), None, None
        delivery.error_message = ""
        delivery.save()
        enqueue_delivery(delivery)
        return "Delivery queued again with its original event ID and timestamp."
    raise ValidationError("Choose a supported connection action.")


def eligible_test_leads(configuration, mapping_id):
    mapping = _row(Mapping, configuration=configuration, pk=mapping_id)
    leads = Lead.objects.filter(organization_id=configuration.organization_id,
                                pipeline_id=mapping.pipeline_id, stage_id=mapping.stage_id)
    if configuration.event_scope == "meta_leads":
        leads = leads.filter(lead_source="meta_ads")
    return [{"id": str(lead.pk), "name": lead.name} for lead in leads[:100]]


def dashboard_state(configuration):
    configuration.refresh_from_db()
    mappings = list(configuration.mappings.select_related("pipeline", "stage"))
    deliveries = configuration.deliveries.filter(organization_id=configuration.organization_id)
    counts = {row["status"]: row["total"] for row in deliveries.values("status").annotate(total=Count("id"))}
    return {
        "configuration": {
            "dataset_id": configuration.dataset_id, "has_access_token": configuration.has_access_token,
            "is_enabled": configuration.is_enabled, "test_mode": configuration.test_mode,
            "test_event_code": configuration.test_event_code, "event_scope": configuration.event_scope,
            "user_data_fields": configuration.user_data_fields,
            "custom_attribute_keys": configuration.custom_attribute_keys,
            "verified_at": configuration.verified_at.isoformat() if configuration.verified_at else None,
        },
        "mappings": [{
            "id": str(m.pk), "pipeline": str(m.pipeline_id), "stage": str(m.stage_id),
            "pipeline_name": m.pipeline.name, "stage_name": m.stage.name,
            "event_name": m.event_name, "is_default": m.is_default, "is_enabled": m.is_enabled,
            "action_source": m.action_source, "value_source": m.value_source,
            "static_value": str(m.static_value) if m.static_value is not None else "",
            "value_attribute": m.value_attribute, "currency": m.currency,
        } for m in mappings],
        "deliveries": [{
            "id": str(d.pk), "event_name": d.payload.get("event_name", ""),
            "event_id": d.event_id, "lead_name": d.lead.name if d.lead else "Deleted lead",
            "is_test": d.is_test, "status": d.status, "attempt_count": d.attempt_count,
            "error_message": d.error_message, "trace_id": d.trace_id,
            "created_at": d.created_at.isoformat(),
        } for d in deliveries.select_related("lead")[:50]],
        "counts": counts,
        "sources": ACTION_SOURCES,
        "currencies": CURRENCIES,
        "fields": USER_DATA_FIELDS,
        "attributes": [{"key": a.key, "name": a.name, "field_type": a.field_type}
                       for a in shareable_attributes(configuration.organization)],
        "numeric_attributes": [{"key": a.key, "name": a.name}
                               for a in shareable_attributes(configuration.organization) if a.field_type == "numeric"],
        "pipelines": [{"id": str(p.pk), "name": p.name, "stages": [
            {"id": str(s.pk), "name": s.name} for s in p.stages.all() if s.is_active
        ]} for p in Pipeline.objects.filter(organization_id=configuration.organization_id, is_active=True).prefetch_related("stages")],
    }
