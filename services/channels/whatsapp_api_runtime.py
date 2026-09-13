"""Runtime hardening for Meta WhatsApp delivery and inbound contact identity."""

from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import json
import re

from celery.signals import task_failure, task_postrun
from django.db import transaction


_INSTALLED = False
_CONTACT_NAMES = ContextVar("shvya_whatsapp_contact_names", default={})
_SEND_TASK_NAME = "apps.channels.tasks.send_whatsapp_message_task"


def normalize_meta_recipient(value) -> str:
    """Meta Cloud API expects the recipient as international digits, without '+'."""
    raw = str(value or "").strip()
    if not raw:
        return raw
    if "@" in raw:
        return raw
    digits = re.sub(r"\D", "", raw)
    if 8 <= len(digits) <= 15 and not re.search(r"[A-Za-z]", raw):
        return digits
    return raw


def _contact_names_from_payload(payload) -> dict[str, str]:
    names: dict[str, str] = {}
    if not isinstance(payload, dict):
        return names
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            value = change.get("value") or {}
            for contact in value.get("contacts") or []:
                if not isinstance(contact, dict):
                    continue
                wa_id = str(contact.get("wa_id") or "").strip()
                profile = contact.get("profile") or {}
                name = " ".join(str(profile.get("name") or "").strip().split())
                if wa_id and name:
                    names[wa_id] = name[:150]
    return names


def _placeholder_lead_name(name, phone="") -> bool:
    value = " ".join(str(name or "").strip().split())
    if not value or value.casefold() in {
        "lead", "whatsapp lead", "whatsapp user", "unknown", "unknown lead"
    }:
        return True
    if "@" in value:
        return True
    value_digits = re.sub(r"\D", "", value)
    phone_digits = re.sub(r"\D", "", str(phone or ""))
    if value_digits and value_digits == phone_digits:
        return True
    return bool(value_digits and value_digits == re.sub(r"\D", "", value) and not re.search(r"[A-Za-z]", value))


def _meta_api_error_text(error) -> str:
    """Keep Meta's actionable error after the sender transaction rolls back.

    The canonical sender holds the WhatsAppMessage row in an atomic transaction.
    A permanent Meta 4xx raises out of that transaction, so DB writes made while
    handling the exception are rolled back.  The Celery post-run signal only sees
    the exception text; make that text carry the safe Meta code/message/details so
    the terminal FAILED write can preserve the real reason.
    """
    status_code = getattr(error, "status_code", None)
    response_body = getattr(error, "response_body", None)
    prefix = (
        f"WhatsApp API returned {status_code}"
        if status_code is not None
        else "WhatsApp API request failed"
    )

    try:
        payload = json.loads(response_body) if isinstance(response_body, str) else response_body
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = None

    meta_error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(meta_error, dict):
        return str(error or prefix)[:1000]

    parts = [prefix]
    code = meta_error.get("code")
    subcode = meta_error.get("error_subcode")
    error_type = str(meta_error.get("type") or "").strip()
    message = str(meta_error.get("message") or "").strip()
    error_data = meta_error.get("error_data") or {}
    details = (
        str(error_data.get("details") or "").strip()
        if isinstance(error_data, dict)
        else ""
    )
    fbtrace_id = str(meta_error.get("fbtrace_id") or "").strip()

    if code not in (None, ""):
        parts.append(f"code {code}")
    if subcode not in (None, ""):
        parts.append(f"subcode {subcode}")
    if error_type:
        parts.append(error_type)
    if details:
        parts.append(details)
    elif message:
        parts.append(message)
    if fbtrace_id:
        parts.append(f"fbtrace_id {fbtrace_id}")

    return "; ".join(parts)[:1000]


def _source_inbound_for_ai_message(message):
    """Return the exact API inbound message that caused an AI outbound reply."""
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    ai_metadata = payload.get("shvya_ai") or {}
    if not isinstance(ai_metadata, dict):
        return None
    source_id = str(ai_metadata.get("source_inbound_message_id") or "").strip()
    if not source_id:
        return None

    from apps.channels.models import WhatsAppAccount, WhatsAppMessage

    return (
        WhatsAppMessage.objects.select_related("account")
        .filter(
            pk=source_id,
            organization_id=message.organization_id,
            lead_id=message.lead_id,
            direction=WhatsAppMessage.Direction.INBOUND,
            account__connection_type=WhatsAppAccount.ConnectionType.API,
        )
        .first()
    )


def _bind_outbound_to_source_api_account(message):
    """Send a 24h AI reply from the same Meta number that received the inbound.

    Meta's customer-service window is scoped to the business/consumer
    conversation.  A recent inbound on API number A does not authorize a free-form
    reply from API number B.  SHVYA previously checked only the inbound timestamp
    and could resolve the outbound sender separately, producing a Meta 400/131047
    even though the lead had just messaged.
    """
    source = _source_inbound_for_ai_message(message)
    if source is None:
        return None

    from apps.channels.models import WhatsAppAccount

    source_account = source.account
    connected = WhatsAppAccount.objects.filter(
        organization_id=message.organization_id,
        connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    )

    target = None
    if source_account.is_active and source_account.status == WhatsAppAccount.Status.CONNECTED:
        target = connected.filter(pk=source_account.pk).first()

    if target is None and source_account.phone_number_id:
        target = (
            connected.filter(phone_number_id=source_account.phone_number_id)
            .order_by("-updated_at", "-connected_at")
            .first()
        )

    if target is None and source_account.display_phone_number:
        target = (
            connected.filter(display_phone_number=source_account.display_phone_number)
            .order_by("-updated_at", "-connected_at")
            .first()
        )

    if target is None:
        return None

    update_fields = []
    if message.account_id != target.id:
        message.account = target
        update_fields.append("account")
    if message.from_number != target.phone_number_id:
        message.from_number = target.phone_number_id
        update_fields.append("from_number")
    if update_fields:
        update_fields.append("updated_at")
        message.save(update_fields=update_fields)

    return target


def _persist_terminal_send_status(message_id, *, error="") -> None:
    from apps.channels.models import WhatsAppMessage
    from services.channels.realtime import publish_status

    message = WhatsAppMessage.objects.filter(pk=message_id).first()
    if message is None:
        return

    update_fields = []
    if message.status == WhatsAppMessage.Status.QUEUED:
        message.status = WhatsAppMessage.Status.FAILED
        update_fields.append("status")
    if error:
        final_error = str(error)[:1000]
        if message.error != final_error:
            message.error = final_error
            update_fields.append("error")
    if update_fields:
        update_fields.append("updated_at")
        message.save(update_fields=update_fields)
    publish_status(message)


def _publish_completed_send_status(message_id) -> None:
    from apps.channels.models import WhatsAppMessage
    from services.channels.realtime import publish_status

    message = WhatsAppMessage.objects.filter(pk=message_id).first()
    if message is not None:
        publish_status(message)


def _send_task_postrun(sender=None, task=None, args=None, kwargs=None, retval=None, state=None, **_extra):
    task_name = getattr(task, "name", None) or getattr(sender, "name", None)
    if task_name != _SEND_TASK_NAME:
        return
    call_args = list(args or [])
    message_id = call_args[0] if call_args else (kwargs or {}).get("message_id")
    if not message_id:
        return
    if isinstance(retval, dict) and retval.get("status") == "failed":
        _persist_terminal_send_status(message_id, error=retval.get("error") or "WhatsApp delivery failed.")
    elif isinstance(retval, dict) and retval.get("status") in {"sent", "skipped"}:
        _publish_completed_send_status(message_id)


def _send_task_failure(sender=None, task_id=None, exception=None, args=None, kwargs=None, **_extra):
    task_name = getattr(sender, "name", None)
    if task_name != _SEND_TASK_NAME:
        return
    call_args = list(args or [])
    message_id = call_args[0] if call_args else (kwargs or {}).get("message_id")
    if message_id:
        _persist_terminal_send_status(message_id, error=str(exception or "WhatsApp delivery failed."))


def install_whatsapp_api_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
    from apps.channels import views_flat
    from services.channels import whatsapp_service

    # Meta provider examples and the Graph API use international digits without
    # a leading '+'. CRM Lead.phone intentionally keeps E.164 '+', so normalize
    # only at the provider boundary and leave CRM identity untouched.
    original_text = WhatsAppClient.send_text_message
    original_template = WhatsAppClient.send_template_message
    original_media = WhatsAppClient.send_media_message

    @wraps(original_text)
    def send_text_message(self, to, body, preview_url=False):
        return original_text(
            self,
            to=normalize_meta_recipient(to),
            body=body,
            preview_url=preview_url,
        )

    @wraps(original_template)
    def send_template_message(self, to, template_name, language_code="en_US", components=None):
        return original_template(
            self,
            to=normalize_meta_recipient(to),
            template_name=template_name,
            language_code=language_code,
            components=components,
        )

    @wraps(original_media)
    def send_media_message(self, *, to, media_type, media_id=None, media_url=None, caption=None, filename=None):
        return original_media(
            self,
            to=normalize_meta_recipient(to),
            media_type=media_type,
            media_id=media_id,
            media_url=media_url,
            caption=caption,
            filename=filename,
        )

    WhatsAppClient.send_text_message = send_text_message
    WhatsAppClient.send_template_message = send_template_message
    WhatsAppClient.send_media_message = send_media_message

    # AI replies inside the 24-hour customer-service window must use the exact
    # API number that received the source inbound message.  Resolve/reconnect to
    # the same Meta phone identity before the provider call rather than trusting
    # a generic organization-level account fallback.
    original_send_outbound = whatsapp_service.send_outbound_message

    @wraps(original_send_outbound)
    def send_outbound_message(*, message):
        _bind_outbound_to_source_api_account(message)
        try:
            return original_send_outbound(message=message)
        except whatsapp_service.WhatsAppSendError as exc:
            cause = exc.__cause__
            if isinstance(cause, WhatsAppAPIError):
                raise whatsapp_service.WhatsAppSendError(
                    _meta_api_error_text(cause)
                ) from cause
            raise

    whatsapp_service.send_outbound_message = send_outbound_message

    # Capture Meta's contact profile name for the duration of the webhook. The
    # canonical inbound service remains the only place that creates/links Leads.
    original_webhook = views_flat.whatsapp_webhook_view

    @wraps(original_webhook)
    def whatsapp_webhook_view(request):
        names = {}
        if getattr(request, "method", "").upper() == "POST":
            try:
                names = _contact_names_from_payload(json.loads(request.body))
            except (TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
                names = {}
        token = _CONTACT_NAMES.set(names)
        try:
            return original_webhook(request)
        finally:
            _CONTACT_NAMES.reset(token)

    views_flat.whatsapp_webhook_view = whatsapp_webhook_view

    original_inbound = whatsapp_service.handle_inbound_message

    @wraps(original_inbound)
    @transaction.atomic
    def handle_inbound_message(*args, **kwargs):
        message = original_inbound(*args, **kwargs)
        if message is None or not getattr(message, "lead_id", None):
            return message
        wa_id = re.sub(r"\D", "", str(kwargs.get("from_number") or ""))
        contact_name = _CONTACT_NAMES.get({}).get(wa_id) or _CONTACT_NAMES.get({}).get(
            str(kwargs.get("from_number") or "").strip()
        )
        lead = getattr(message, "lead", None)
        if contact_name and lead is not None and _placeholder_lead_name(lead.name, lead.phone):
            lead.name = contact_name[:150]
            lead.save(update_fields=["name", "updated_at"])
        return message

    whatsapp_service.handle_inbound_message = handle_inbound_message

    # A permanent Meta error is raised from inside the sender's atomic row lock.
    # That rollback can otherwise restore the DB row to QUEUED even though the
    # send task already returned a terminal failure. Persist the final task
    # outcome outside that transaction and publish it to the open inbox.
    task_postrun.connect(
        _send_task_postrun,
        weak=False,
        dispatch_uid="shvya.whatsapp.send.postrun.status.v1",
    )
    task_failure.connect(
        _send_task_failure,
        weak=False,
        dispatch_uid="shvya.whatsapp.send.failure.status.v1",
    )

    _INSTALLED = True
