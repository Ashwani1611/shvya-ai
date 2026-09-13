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


def _persist_terminal_send_status(message_id, *, error="") -> None:
    from apps.channels.models import WhatsAppMessage
    from services.channels.realtime import publish_status

    message = WhatsAppMessage.objects.filter(pk=message_id).first()
    if message is None:
        return
    if message.status == WhatsAppMessage.Status.QUEUED:
        message.status = WhatsAppMessage.Status.FAILED
        if error and not message.error:
            message.error = str(error)[:1000]
        message.save(update_fields=["status", "error", "updated_at"])
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

    from apps.channels.providers.whatsapp import WhatsAppClient
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
