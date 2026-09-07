"""Business logic for linked-device (hosted) WhatsApp sessions.

Django remains the CRM/control plane. The separate Node gateway owns the
whatsapp-web.js browser sessions. This module owns tenant-safe number mapping,
settings, inbound persistence, and hosted conversation helpers.
"""

from copy import deepcopy
from datetime import datetime, timezone as dt_timezone

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.crm.lead_service import upsert_lead


HOSTED_CONNECTION_TYPE = "hosted"

DEFAULT_SESSION_SETTINGS = {
    "ai_auto_reply": False,
    "auto_lead_creation": False,
    "bump_up_messages": False,
    "bump_up_count": 2,
    "auto_follow_up": True,
    "business_hours_start": "10:30",
    "business_hours_end": "20:00",
    "active_conversation_delay_value": 2,
    "active_conversation_delay_unit": "hours",
}


class HostedWhatsAppValidationError(ValueError):
    pass


def normalize_whatsapp_number(*, country_code="", phone_number=""):
    """Return an E.164-ish `+digits` representation used for comparisons."""
    raw_phone = str(phone_number or "").strip()
    raw_country = str(country_code or "").strip()

    phone_digits = "".join(ch for ch in raw_phone if ch.isdigit())
    country_digits = "".join(ch for ch in raw_country if ch.isdigit())

    if not phone_digits:
        return ""

    if raw_phone.startswith("+"):
        combined = phone_digits
    elif country_digits and phone_digits.startswith(country_digits):
        combined = phone_digits
    else:
        combined = f"{country_digits}{phone_digits}"

    if len(combined) < 8 or len(combined) > 15:
        return ""

    return f"+{combined}"


def pipeline_whatsapp_number(pipeline):
    return normalize_whatsapp_number(
        country_code=pipeline.country_code,
        phone_number=pipeline.phone_number,
    )


def resolve_pipeline_for_number(*, organization, phone_number):
    normalized = normalize_whatsapp_number(phone_number=phone_number)
    if not normalized:
        return None

    for pipeline in Pipeline.objects.filter(
        organization=organization,
        is_active=True,
    ).select_related("owner"):
        if pipeline_whatsapp_number(pipeline) == normalized:
            return pipeline
    return None


def require_pipeline_number(*, organization, country_code, phone_number):
    normalized = normalize_whatsapp_number(
        country_code=country_code,
        phone_number=phone_number,
    )
    if not normalized:
        raise HostedWhatsAppValidationError(
            "Enter a valid WhatsApp number including the country code."
        )

    pipeline = resolve_pipeline_for_number(
        organization=organization,
        phone_number=normalized,
    )
    if not pipeline:
        raise HostedWhatsAppValidationError(
            "This number is not linked to an active pipeline. Map the same "
            "WhatsApp number to a pipeline first, then create the session."
        )
    return normalized, pipeline


@transaction.atomic
def create_hosted_account(
    *,
    organization,
    created_by,
    country_code,
    phone_number,
):
    normalized, pipeline = require_pipeline_number(
        organization=organization,
        country_code=country_code,
        phone_number=phone_number,
    )

    existing = WhatsAppAccount.objects.filter(
        organization=organization,
        connection_type=HOSTED_CONNECTION_TYPE,
        display_phone_number=normalized,
        is_active=True,
    ).first()
    if existing:
        existing.status = WhatsAppAccount.Status.PENDING
        existing.business_name = pipeline.name
        existing.phone_number_id = normalized
        existing.save(
            update_fields=[
                "status",
                "business_name",
                "phone_number_id",
                "updated_at",
            ]
        )
        ensure_session_settings(account=existing)
        return existing, pipeline, False

    account = WhatsAppAccount.objects.create(
        organization=organization,
        connection_type=HOSTED_CONNECTION_TYPE,
        business_name=pipeline.name,
        phone_number_id=normalized,
        display_phone_number=normalized,
        status=WhatsAppAccount.Status.PENDING,
        is_active=True,
    )
    ensure_session_settings(account=account)
    return account, pipeline, True


def get_pipeline_for_account(*, account):
    return resolve_pipeline_for_number(
        organization=account.organization,
        phone_number=account.display_phone_number or account.phone_number_id,
    )


def _settings_root(settings):
    root = settings.setdefault("hosted_whatsapp", {})
    return root.setdefault("sessions", {})


@transaction.atomic
def ensure_session_settings(*, account):
    organization = Organization.objects.select_for_update().get(
        pk=account.organization_id
    )
    org_settings = deepcopy(organization.settings or {})
    sessions = _settings_root(org_settings)
    key = str(account.id)
    current = sessions.get(key, {})
    merged = {**DEFAULT_SESSION_SETTINGS, **current}
    sessions[key] = merged
    organization.settings = org_settings
    organization.save(update_fields=["settings", "updated_at"])
    account.organization = organization
    return deepcopy(merged)


def get_session_settings(*, account):
    org_settings = account.organization.settings or {}
    sessions = org_settings.get("hosted_whatsapp", {}).get("sessions", {})
    return {
        **DEFAULT_SESSION_SETTINGS,
        **deepcopy(sessions.get(str(account.id), {})),
    }


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


@transaction.atomic
def update_session_settings(*, account, payload):
    organization = Organization.objects.select_for_update().get(
        pk=account.organization_id
    )
    org_settings = deepcopy(organization.settings or {})
    sessions = _settings_root(org_settings)
    current = {
        **DEFAULT_SESSION_SETTINGS,
        **sessions.get(str(account.id), {}),
    }

    for key in (
        "ai_auto_reply",
        "auto_lead_creation",
        "bump_up_messages",
        "auto_follow_up",
    ):
        if key in payload:
            current[key] = _as_bool(payload.get(key))

    try:
        bump_count = int(payload.get("bump_up_count", current["bump_up_count"]))
    except (TypeError, ValueError):
        raise HostedWhatsAppValidationError(
            "Number of bump-up messages must be a number."
        )
    if bump_count < 1 or bump_count > 10:
        raise HostedWhatsAppValidationError(
            "Number of bump-up messages must be between 1 and 10."
        )
    current["bump_up_count"] = bump_count

    for key in ("business_hours_start", "business_hours_end"):
        value = str(payload.get(key, current[key])).strip()
        if len(value) != 5 or value[2] != ":":
            raise HostedWhatsAppValidationError(
                "Business hours must use HH:MM format."
            )
        try:
            hour, minute = (int(part) for part in value.split(":"))
        except ValueError:
            raise HostedWhatsAppValidationError(
                "Business hours must use HH:MM format."
            )
        if hour not in range(24) or minute not in range(60):
            raise HostedWhatsAppValidationError("Invalid business hour value.")
        current[key] = value

    try:
        delay_value = int(
            payload.get(
                "active_conversation_delay_value",
                current["active_conversation_delay_value"],
            )
        )
    except (TypeError, ValueError):
        raise HostedWhatsAppValidationError(
            "Conversation delay must be a number."
        )
    if delay_value < 1 or delay_value > 168:
        raise HostedWhatsAppValidationError(
            "Conversation delay must be between 1 and 168."
        )
    delay_unit = str(
        payload.get(
            "active_conversation_delay_unit",
            current["active_conversation_delay_unit"],
        )
    )
    if delay_unit not in {"minutes", "hours", "days"}:
        raise HostedWhatsAppValidationError("Invalid conversation delay unit.")
    current["active_conversation_delay_value"] = delay_value
    current["active_conversation_delay_unit"] = delay_unit

    sessions[str(account.id)] = current
    organization.settings = org_settings
    organization.save(update_fields=["settings", "updated_at"])
    account.organization = organization
    return deepcopy(current)


def _first_stage(pipeline):
    if not pipeline:
        return None
    return Stage.objects.filter(
        pipeline=pipeline,
        is_active=True,
    ).order_by("display_order").first()


def _normalize_contact_number(value):
    value = str(value or "").strip()
    if value.endswith("@lid"):
        return ""
    if "@" in value:
        value = value.split("@", 1)[0]
    return normalize_whatsapp_number(phone_number=value)


def _message_timestamp(payload):
    try:
        timestamp = float(payload.get("timestamp") or 0)
    except (TypeError, ValueError):
        return None
    if timestamp <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp, tz=dt_timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _message_type(payload):
    message_type = str(payload.get("messageType") or "text").lower()
    allowed = {choice for choice, _label in WhatsAppMessage.MessageType.choices}
    if message_type not in allowed:
        return WhatsAppMessage.MessageType.TEXT
    return message_type


def _outbound_status(payload):
    status = str(payload.get("status") or "").lower()
    return {
        "sent": WhatsAppMessage.Status.SENT,
        "delivered": WhatsAppMessage.Status.DELIVERED,
        "read": WhatsAppMessage.Status.READ,
        "failed": WhatsAppMessage.Status.FAILED,
    }.get(status, WhatsAppMessage.Status.SENT)


def _persist_gateway_message(*, account, payload, historical=False):
    raw_message_id = payload.get("messageId")
    if not raw_message_id:
        return None

    external_id = f"wweb:{raw_message_id}"
    is_group = bool(payload.get("isGroup"))
    is_outbound = bool(payload.get("fromMe"))
    account_number = normalize_whatsapp_number(
        phone_number=account.display_phone_number or account.phone_number_id
    )
    chat_id = str(payload.get("chatId") or "")

    if is_group:
        peer = chat_id or str(payload.get("from") or payload.get("to") or "")
    elif is_outbound:
        peer = _normalize_contact_number(
            payload.get("contactPhoneNumber") or payload.get("to") or chat_id
        )
    else:
        peer = _normalize_contact_number(
            payload.get("contactPhoneNumber") or payload.get("from") or chat_id
        )

    if not is_group and not is_outbound and not peer and chat_id:
        # WhatsApp may expose a privacy @lid identifier. A previous ignore-list
        # snapshot stores both its chat id and resolved phone number, so reuse
        # that mapping if live contact resolution is temporarily unavailable.
        from apps.channels.hosted_ignore_models import HostedChatIgnoreContact

        matched_snapshot = HostedChatIgnoreContact.objects.filter(
            organization=account.organization,
            account=account,
            chat_id=chat_id,
        ).first()
        if matched_snapshot:
            peer = matched_snapshot.phone_number

    direction = (
        WhatsAppMessage.Direction.OUTBOUND
        if is_outbound
        else WhatsAppMessage.Direction.INBOUND
    )
    from_number = account_number if is_outbound else peer
    to_number = peer if is_outbound else account_number

    lead = None
    if not is_group and peer:
        lead = Lead.objects.filter(
            organization=account.organization,
            phone=peer,
        ).first()

    settings = get_session_settings(account=account)
    pipeline = get_pipeline_for_account(account=account)
    ignored_existing_chat = False
    if (
        not is_outbound
        and not historical
        and not lead
        and peer
        and settings["auto_lead_creation"]
    ):
        from services.channels.hosted_ignore_service import (
            is_hosted_contact_ignored,
        )

        ignored_existing_chat = is_hosted_contact_ignored(
            account=account,
            phone_number=peer,
        )

    if (
        not is_outbound
        and not historical
        and not lead
        and peer
        and not ignored_existing_chat
        and settings["auto_lead_creation"]
        and pipeline
    ):
        stage = _first_stage(pipeline)
        if stage:
            try:
                lead, _created = upsert_lead(
                    organization=account.organization,
                    pipeline=pipeline,
                    stage=stage,
                    name=payload.get("contactName") or peer,
                    phone=peer,
                    lead_source="whatsapp_api",
                )
            except ValidationError:
                lead = Lead.objects.filter(
                    organization=account.organization,
                    phone=peer,
                ).first()

    defaults = {
        "organization": account.organization,
        "account": account,
        "lead": lead,
        "direction": direction,
        "from_number": from_number or "unknown",
        "to_number": peer if is_outbound else account_number or "unknown",
        "body": str(payload.get("body") or ""),
        "message_type": _message_type(payload),
        "status": (
            _outbound_status(payload)
            if is_outbound
            else WhatsAppMessage.Status.RECEIVED
        ),
        "raw_payload": {
            **payload,
            "isHistory": bool(historical),
            "ignoredExistingChat": bool(ignored_existing_chat),
        },
        "is_read": True if is_outbound or historical else False,
    }
    message, created = WhatsAppMessage.objects.get_or_create(
        external_id=external_id,
        defaults=defaults,
    )
    if not created:
        return message

    occurred_at = _message_timestamp(payload)
    if occurred_at:
        WhatsAppMessage.objects.filter(pk=message.pk).update(created_at=occurred_at)
        message.created_at = occurred_at

    if (
        lead
        and not is_outbound
        and not historical
        and settings["ai_auto_reply"]
    ):
        lead_id = str(lead.id)

        def queue_ai_reply():
            from apps.ai_engagement.tasks import generate_ai_engagement_response

            generate_ai_engagement_response.delay(lead_id)

        transaction.on_commit(queue_ai_reply)

    return message


@transaction.atomic
def handle_gateway_event(*, payload):
    """Apply one authenticated callback from the hosted gateway."""
    session_id = payload.get("sessionId")
    event = str(payload.get("event") or "").strip().lower()
    if not session_id or not event:
        return None

    account = WhatsAppAccount.objects.select_related("organization").filter(
        id=session_id,
        connection_type=HOSTED_CONNECTION_TYPE,
        is_active=True,
    ).first()
    if not account:
        return None

    if event in {"qr", "connecting", "authenticated", "syncing"}:
        if account.status != WhatsAppAccount.Status.PENDING:
            account.status = WhatsAppAccount.Status.PENDING
            account.save(update_fields=["status", "updated_at"])
        return account

    if event in {"ready", "running"}:
        connected_number = _normalize_contact_number(payload.get("phoneNumber"))
        expected = normalize_whatsapp_number(
            phone_number=account.display_phone_number
        )
        if connected_number and expected and connected_number != expected:
            # The gateway also logs this linked device out. Persist FAILED and
            # return normally so the surrounding atomic callback commits it.
            account.status = WhatsAppAccount.Status.FAILED
            account.save(update_fields=["status", "updated_at"])
            return account
        account.status = WhatsAppAccount.Status.CONNECTED
        if connected_number:
            account.display_phone_number = connected_number
            account.phone_number_id = connected_number
            account.save(
                update_fields=[
                    "status",
                    "display_phone_number",
                    "phone_number_id",
                    "updated_at",
                ]
            )
        else:
            account.save(update_fields=["status", "updated_at"])
        return account

    if event in {"disconnected", "logout"}:
        account.status = WhatsAppAccount.Status.DISCONNECTED
        account.save(update_fields=["status", "updated_at"])
        return account

    if event in {"auth_failure", "failed"}:
        account.status = WhatsAppAccount.Status.FAILED
        account.save(update_fields=["status", "updated_at"])
        return account

    if event == "message_ack":
        external_id = payload.get("messageId")
        if not external_id:
            return None
        external_id = f"wweb:{external_id}"
        status = str(payload.get("status") or "").lower()
        mapped = {
            "sent": WhatsAppMessage.Status.SENT,
            "delivered": WhatsAppMessage.Status.DELIVERED,
            "read": WhatsAppMessage.Status.READ,
            "failed": WhatsAppMessage.Status.FAILED,
        }.get(status)
        if not mapped:
            return None
        message = WhatsAppMessage.objects.filter(
            organization=account.organization,
            account=account,
            external_id=external_id,
        ).first()
        if message:
            message.status = mapped
            message.raw_payload = payload
            message.save(update_fields=["status", "raw_payload", "updated_at"])
        return message

    if event == "history_sync":
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return None
        last_message = None
        for item in messages:
            if not isinstance(item, dict):
                continue
            last_message = _persist_gateway_message(
                account=account,
                payload=item,
                historical=True,
            ) or last_message
        return last_message or account

    if event != "message":
        return None

    return _persist_gateway_message(
        account=account,
        payload=payload,
        historical=False,
    )


def queue_hosted_text_message(*, account, to_number, body, lead=None, metadata=None):
    raw_to = str(to_number or "").strip()
    if raw_to.endswith("@g.us"):
        normalized_to = raw_to
    else:
        normalized_to = normalize_whatsapp_number(phone_number=raw_to)
        if not normalized_to:
            raise HostedWhatsAppValidationError(
                "Enter a valid WhatsApp recipient number."
            )
    if not str(body or "").strip():
        raise HostedWhatsAppValidationError("Message cannot be empty.")

    return WhatsAppMessage.objects.create(
        organization=account.organization,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        from_number=account.display_phone_number or account.phone_number_id,
        to_number=normalized_to,
        body=str(body).strip(),
        message_type=WhatsAppMessage.MessageType.TEXT,
        status=WhatsAppMessage.Status.QUEUED,
        raw_payload={"shvya_hosted": metadata or {"origin": "agent"}},
    )


def queued_messages(*, account):
    return WhatsAppMessage.objects.filter(
        organization=account.organization,
        account=account,
        direction=WhatsAppMessage.Direction.OUTBOUND,
        status=WhatsAppMessage.Status.QUEUED,
    ).select_related("lead").order_by("created_at")