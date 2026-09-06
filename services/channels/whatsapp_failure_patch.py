"""Compatibility layer that preserves and renders WhatsApp failure details."""

from functools import wraps

from django.utils.html import format_html

from apps.channels.models import WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError

from . import whatsapp_service as base
from .whatsapp_error_service import (
    describe_whatsapp_failure,
    failure_summary,
    merge_api_error_payload,
    message_failure_details,
)

_INSTALLED = False


def _failure_block(details):
    """Return safe, compact HTML for the existing failed-message details box."""
    code = details.get("code") or ""
    title = details.get("title") or "WhatsApp Delivery Failed"
    heading = f"{code} — {title}" if code else title
    why = details.get("why") or "Meta reported that WhatsApp could not deliver this message."
    resolve = details.get("resolve") or "Review the WhatsApp configuration and retry."
    meta_message = details.get("meta_message") or ""

    meta_html = ""
    if meta_message and meta_message.strip().lower() != why.strip().lower():
        meta_html = format_html(
            '<div class="mt-1.5 border-t border-red-100 pt-1.5 text-[10px] text-red-500">'
            '<span class="font-semibold">Meta:</span> {}</div>',
            meta_message,
        )

    return format_html(
        '<div class="font-semibold text-red-700">{}</div>'
        '<div class="mt-1"><span class="font-semibold">Why:</span> {}</div>'
        '<div class="mt-1"><span class="font-semibold">Resolve:</span> {}</div>{}',
        heading,
        why,
        resolve,
        meta_html,
    )


def _persist_failure(message, *, response_body=None, error_text=""):
    payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
    if response_body:
        payload = merge_api_error_payload(payload, response_body)

    details = describe_whatsapp_failure(
        raw_payload=payload,
        error_text=error_text or message.error or "",
    )
    summary = failure_summary(details)

    update_fields = []
    if payload != message.raw_payload:
        message.raw_payload = payload
        update_fields.append("raw_payload")
    if message.error != summary:
        message.error = summary
        update_fields.append("error")
    if update_fields:
        update_fields.append("updated_at")
        message.save(update_fields=update_fields)
    return details


def install_whatsapp_failure_diagnostics():
    """Install once during ChannelsConfig.ready()."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_status_update = base.handle_status_update
    original_send = base.send_outbound_message
    original_get_messages = base.get_conversation_messages

    @wraps(original_status_update)
    def handle_status_update(*, external_id, status, raw_payload):
        result = original_status_update(
            external_id=external_id,
            status=status,
            raw_payload=raw_payload,
        )
        if not result:
            return result

        message = WhatsAppMessage.objects.filter(external_id=external_id).first()
        if not message:
            return result

        if message.status == WhatsAppMessage.Status.FAILED:
            _persist_failure(message)
        elif message.error:
            message.error = ""
            message.save(update_fields=["error", "updated_at"])
        return result

    @wraps(original_send)
    def send_outbound_message(*, message):
        try:
            return original_send(message=message)
        except base.WhatsAppSendError as exc:
            cause = exc.__cause__
            response_body = (
                cause.response_body
                if isinstance(cause, WhatsAppAPIError)
                else None
            )
            _persist_failure(
                message,
                response_body=response_body,
                error_text=str(exc),
            )
            raise

    @wraps(original_get_messages)
    def get_conversation_messages(*, organization, lead, account=None):
        messages = list(
            original_get_messages(
                organization=organization,
                lead=lead,
                account=account,
            )
        )
        for message in messages:
            if message.status != WhatsAppMessage.Status.FAILED:
                continue
            details = message_failure_details(message)
            # Only the in-memory value is replaced with formatted HTML. The DB
            # keeps a short searchable error summary such as "131049 — ...".
            message.error = _failure_block(details)
        return messages

    base.handle_status_update = handle_status_update
    base.send_outbound_message = send_outbound_message
    base.get_conversation_messages = get_conversation_messages
    _INSTALLED = True
