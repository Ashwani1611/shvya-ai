"""Process partner-level WhatsApp Coexistence sync events.

Meta can deliver the one-time ``history`` and ``smb_app_state_sync`` payloads in
a partner event envelope shaped like ``{id, event, data}`` rather than the
normal Cloud API ``entry[].changes[]`` envelope. The existing Coexistence
runtime already handles the latter. This layer is installed after it and only
handles the partner envelope, after the normal webhook path has returned 200
(which means HMAC verification already succeeded).
"""

import json
import logging
from functools import wraps

from apps.channels import views_flat
from apps.channels.models import WhatsAppAccount
from services.channels.whatsapp_coexistence_service import (
    handle_history_sync,
    handle_smb_app_state_sync,
    handle_smb_message_echoes,
)

logger = logging.getLogger(__name__)
_INSTALLED = False
_PARTNER_EVENTS = {"history", "smb_app_state_sync", "smb_message_echoes"}


def _account_for_partner_event(data):
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata") or {}
    phone_number_id = str(metadata.get("phone_number_id") or "").strip()
    waba_id = str(data.get("id") or "").strip()

    accounts = WhatsAppAccount.objects.filter(
        connection_type=WhatsAppAccount.ConnectionType.API,
        status=WhatsAppAccount.Status.CONNECTED,
        is_active=True,
    ).select_related("organization")
    if phone_number_id:
        accounts = accounts.filter(phone_number_id=phone_number_id)
    elif waba_id:
        accounts = accounts.filter(waba_id=waba_id)
    else:
        return None
    return accounts.first()


def process_partner_coexistence_event(payload):
    """Process one top-level Coexistence event; return True when recognized."""
    if not isinstance(payload, dict):
        return False
    event = str(payload.get("event") or "").strip().lower()
    if event not in _PARTNER_EVENTS:
        return False
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return True

    account = _account_for_partner_event(data)
    if account is None:
        metadata = data.get("metadata") or {}
        logger.warning(
            "Partner Coexistence event has no connected SHVYA account: event=%s phone=%s waba=%s",
            event,
            metadata.get("phone_number_id"),
            data.get("id"),
        )
        return True

    if event == "history":
        saved = handle_history_sync(account=account, value=data)
        logger.info(
            "Imported partner Coexistence history: account=%s messages=%s",
            account.id,
            len(saved),
        )
    elif event == "smb_app_state_sync":
        updated = handle_smb_app_state_sync(account=account, value=data)
        logger.info(
            "Applied partner Coexistence contact sync: account=%s updated=%s",
            account.id,
            updated,
        )
    else:
        saved = handle_smb_message_echoes(account=account, value=data)
        logger.info(
            "Imported partner Coexistence message echoes: account=%s messages=%s",
            account.id,
            len(saved),
        )
    return True


def install_whatsapp_coexistence_partner_runtime():
    """Extend the verified webhook with top-level partner event handling."""
    global _INSTALLED
    if _INSTALLED:
        return

    original_delivery = views_flat._handle_webhook_delivery

    @wraps(original_delivery)
    def handle_webhook_delivery(request):
        response = original_delivery(request)
        if getattr(response, "status_code", 500) != 200:
            return response
        try:
            payload = json.loads(request.body)
        except (TypeError, ValueError, json.JSONDecodeError):
            return response
        try:
            process_partner_coexistence_event(payload)
        except Exception:
            # The verified webhook has already returned a successful result for
            # normal WhatsApp processing. Keep Meta's delivery idempotent and
            # avoid retry storms while surfacing the importer failure in logs.
            logger.exception("Partner WhatsApp Coexistence event processing failed.")
        return response

    views_flat._handle_webhook_delivery = handle_webhook_delivery
    _INSTALLED = True
