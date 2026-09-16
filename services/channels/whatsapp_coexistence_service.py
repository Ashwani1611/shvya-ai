"""WhatsApp Business App Coexistence onboarding and webhook helpers.

Coexistence uses Meta Cloud API for transport, just like SHVYA's existing
Connect API accounts. The difference is onboarding: an already-active
WhatsApp Business App number must *not* be registered again with Cloud API.
Meta's dedicated Embedded Signup flow links the Business App and Cloud API,
then exposes contact/history synchronization plus Business-App message echoes.
"""

import logging

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers import whatsapp as whatsapp_provider
from apps.channels.providers import whatsapp_embedded as embedded_provider
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.crm.models import Lead, Stage
from services.channels.embedded_signup_service import (
    EmbeddedSignupError,
    EmbeddedSignupPhoneSelectionRequired,
    _meta_error_details,
    _resolve_signup_assets,
    _update_attempt,
)
from services.crm.lead_service import upsert_lead

logger = logging.getLogger(__name__)

COEXISTENCE_FEATURE_TYPE = "whatsapp_business_app_onboarding"
COEXISTENCE_FINISH_EVENT = "FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING"
COEXISTENCE_WEBHOOK_FIELDS = {"history", "smb_app_state_sync", "smb_message_echoes"}


def _provider_error(exc, *, stage):
    code, reason = _meta_error_details(exc)
    return EmbeddedSignupError(reason, stage=stage, meta_error_code=code)


def _phone_details_without_registration(*, phone_number_id, access_token):
    """Fetch phone metadata without invoking SHVYA's Cloud-API register hook.

    ``install_whatsapp_phone_registration`` wraps the provider globally for the
    standard Embedded Signup flow. Coexistence numbers are already active in
    WhatsApp Business App and Meta's Coexistence flow performs the linking, so
    calling /register here would break the intended setup.
    """
    from services.channels import whatsapp_phone_registration

    getter = whatsapp_phone_registration._ORIGINAL_GET_PHONE_NUMBER_DETAILS
    if getter is None:
        # During isolated unit tests AppConfig.ready() may not have installed
        # the wrapper yet. In that case the provider is already the raw getter.
        getter = whatsapp_provider.get_phone_number_details
    return getter(phone_number_id=phone_number_id, access_token=access_token)


def _coexistence_status(*, phone_number_id, access_token):
    """Best-effort verification that Meta reports this phone on Business App."""
    url = f"{whatsapp_provider.GRAPH_API_BASE}/{phone_number_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "fields": "display_phone_number,verified_name,is_on_biz_app,platform_type",
    }
    try:
        response = requests.get(
            url,
            headers=headers,
            params=params,
            timeout=whatsapp_provider.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        logger.warning(
            "Coexistence status lookup failed due to network error: phone_number_id=%s",
            phone_number_id,
            exc_info=True,
        )
        return {}

    if not response.ok:
        # Some Graph versions/accounts do not expose the diagnostic fields yet.
        # The dedicated Embedded Signup completion event remains authoritative.
        logger.info(
            "Coexistence status fields unavailable: phone_number_id=%s status=%s",
            phone_number_id,
            response.status_code,
        )
        return {}
    try:
        return response.json()
    except ValueError:
        return {}


def request_smb_app_data_sync(*, phone_number_id, access_token, sync_type):
    """Request Meta's one-time Coexistence contacts/history synchronization."""
    if sync_type not in {"history", "smb_app_state_sync"}:
        raise ValueError("Unsupported Coexistence sync_type")

    url = f"{whatsapp_provider.GRAPH_API_BASE}/{phone_number_id}/smb_app_data"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"messaging_product": "whatsapp", "sync_type": sync_type}
    try:
        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=whatsapp_provider.REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise WhatsAppAPIError(
            f"Network error requesting WhatsApp Coexistence {sync_type} sync: {exc}"
        ) from exc

    if not response.ok:
        raise WhatsAppAPIError(
            f"Meta Coexistence {sync_type} sync returned {response.status_code}",
            status_code=response.status_code,
            response_body=response.text,
        )
    try:
        return response.json()
    except ValueError as exc:
        raise WhatsAppAPIError(
            f"Meta Coexistence {sync_type} sync returned invalid JSON.",
            status_code=response.status_code,
            response_body=response.text,
        ) from exc


def complete_coexistence_signup(
    *,
    organization,
    code,
    waba_id="",
    phone_number_id="",
    attempt=None,
):
    """Complete Meta Coexistence onboarding without registering the phone again."""
    if not settings.META_APP_ID or not settings.META_APP_SECRET:
        raise EmbeddedSignupError(
            "WhatsApp Coexistence is not configured on this server yet.",
            stage="server_configuration",
        )
    if not code:
        raise EmbeddedSignupError(
            "Meta did not return an authorization code. Start Coexistence setup again.",
            stage="token_exchange",
        )

    try:
        access_token = embedded_provider.exchange_code_for_access_token(
            app_id=settings.META_APP_ID,
            app_secret=settings.META_APP_SECRET,
            code=code,
            redirect_uri="",
        )
    except WhatsAppAPIError as exc:
        raise _provider_error(exc, stage="token_exchange") from exc

    _update_attempt(
        attempt,
        status="token_exchanged",
        stage="token_exchange",
        token_received=True,
        meta_error_code="",
        error_message="",
    )

    try:
        waba_id, phone_number_id = _resolve_signup_assets(
            access_token=access_token,
            waba_id=waba_id,
            phone_number_id=phone_number_id,
        )
    except EmbeddedSignupPhoneSelectionRequired:
        # Coexistence v3 commonly omits phone_number_id from the browser event.
        # Server-side discovery normally resolves one phone. If the authorized
        # WABA exposes several, asking the customer to choose would require us
        # to retain the business token. Fail safely and ask them to rerun the
        # dedicated flow selecting only the intended Business App number.
        raise EmbeddedSignupError(
            "Meta authorized more than one WhatsApp phone number and did not identify "
            "the Coexistence number. Please run Coexistence setup again and select "
            "the intended WhatsApp Business App number.",
            stage="asset_discovery",
        )

    _update_attempt(
        attempt,
        stage="asset_discovery",
        waba_id=waba_id,
        phone_number_id=phone_number_id,
    )

    try:
        phone_details = _phone_details_without_registration(
            phone_number_id=phone_number_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        raise _provider_error(exc, stage="phone_lookup") from exc

    status_details = _coexistence_status(
        phone_number_id=phone_number_id,
        access_token=access_token,
    )
    if status_details.get("is_on_biz_app") is False:
        raise EmbeddedSignupError(
            "Meta reports that this number is not connected to WhatsApp Business App. "
            "Use Connect WhatsApp Business API for a Cloud-API-only number, or rerun "
            "Coexistence with the number currently active in WhatsApp Business App.",
            stage="coexistence_validation",
        )

    display_phone_number = phone_details.get("display_phone_number", "") or ""
    business_name = phone_details.get("verified_name", "") or ""
    _update_attempt(
        attempt,
        status="phone_verified",
        stage="phone_lookup",
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        display_phone_number=display_phone_number,
        business_name=business_name,
    )

    try:
        with transaction.atomic():
            account = (
                WhatsAppAccount.objects.select_for_update()
                .filter(organization=organization, phone_number_id=phone_number_id)
                .order_by("-is_active", "-updated_at", "-connected_at")
                .first()
            )
            if account is None:
                account = WhatsAppAccount.objects.create(
                    organization=organization,
                    # Runtime is intentionally API: Coexistence uses Meta Cloud API,
                    # never the hosted whatsapp-web.js linked-device transport.
                    connection_type=WhatsAppAccount.ConnectionType.API,
                    waba_id=waba_id,
                    phone_number_id=phone_number_id,
                    display_phone_number=display_phone_number,
                    business_name=business_name,
                    access_token=access_token,
                    status=WhatsAppAccount.Status.CONNECTED,
                    is_active=True,
                )
            else:
                account.connection_type = WhatsAppAccount.ConnectionType.API
                account.waba_id = waba_id
                account.display_phone_number = display_phone_number
                account.business_name = business_name
                account.access_token = access_token
                account.status = WhatsAppAccount.Status.CONNECTED
                account.is_active = True
                account.save(
                    update_fields=[
                        "connection_type",
                        "waba_id",
                        "display_phone_number",
                        "business_name",
                        "access_token",
                        "status",
                        "is_active",
                        "updated_at",
                    ]
                )

            WhatsAppAccount.objects.filter(
                organization=organization,
                phone_number_id=phone_number_id,
            ).exclude(id=account.id).update(
                status=WhatsAppAccount.Status.DISCONNECTED,
                is_active=False,
            )
    except Exception as exc:
        logger.exception(
            "Could not persist WhatsApp Coexistence account: org=%s phone=%s",
            organization.id,
            phone_number_id,
        )
        raise EmbeddedSignupError(
            "SHVYA received the Meta Coexistence connection but could not save it.",
            stage="account_save",
        ) from exc

    _update_attempt(
        attempt,
        account=account,
        stage="account_saved",
        waba_id=account.waba_id,
        phone_number_id=account.phone_number_id,
        display_phone_number=account.display_phone_number,
        business_name=account.business_name,
    )

    warnings = []
    try:
        whatsapp_provider.subscribe_app_to_waba(
            waba_id=waba_id,
            access_token=access_token,
        )
    except WhatsAppAPIError as exc:
        code, reason = _meta_error_details(exc)
        warnings.append(f"Webhook subscription needs attention: {reason}")
        logger.warning(
            "Coexistence WABA subscription failed: account=%s code=%s reason=%s",
            account.id,
            code,
            reason,
        )

    sync_results = {}
    # Meta allows these one-time synchronization requests only after successful
    # Coexistence onboarding, so trigger them immediately while the grant is fresh.
    for sync_type in ("smb_app_state_sync", "history"):
        try:
            sync_results[sync_type] = request_smb_app_data_sync(
                phone_number_id=phone_number_id,
                access_token=access_token,
                sync_type=sync_type,
            )
        except WhatsAppAPIError as exc:
            code, reason = _meta_error_details(exc)
            warnings.append(f"{sync_type} sync could not be started: {reason}")
            logger.warning(
                "Coexistence sync request failed: account=%s type=%s code=%s reason=%s",
                account.id,
                sync_type,
                code,
                reason,
            )

    warning = " ".join(warnings)
    _update_attempt(
        attempt,
        status="connected",
        stage="connected" if not warning else "coexistence_sync_warning",
        webhook_subscribed=not any("Webhook subscription" in item for item in warnings),
        warning_message=warning,
        completed_at=timezone.now(),
    )
    return account, warning, sync_results


def _normalize_phone(value):
    value = str(value or "").strip()
    if not value:
        return ""
    return value if value.startswith("+") else f"+{value}"


def _ensure_lead_for_phone(*, account, phone, name=""):
    normalized = _normalize_phone(phone)
    if not normalized:
        return None
    lead = Lead.objects.filter(
        organization=account.organization,
        phone=normalized,
    ).first()
    if lead:
        if name and (not lead.name or lead.name in {phone, normalized}):
            lead.name = name
            lead.save(update_fields=["name", "updated_at"])
        return lead

    from services.channels.whatsapp_service import resolve_pipeline

    pipeline = resolve_pipeline(
        organization=account.organization,
        to_number=account.display_phone_number or account.phone_number_id,
    )
    if not pipeline:
        return None
    stage = Stage.objects.filter(
        pipeline=pipeline,
        is_active=True,
    ).order_by("display_order").first()
    if not stage:
        return None
    try:
        lead, _ = upsert_lead(
            organization=account.organization,
            pipeline=pipeline,
            stage=stage,
            name=name or phone,
            phone=normalized,
            lead_source="whatsapp_api",
        )
        return lead
    except Exception:
        logger.exception(
            "Could not create lead while importing Coexistence conversation: account=%s phone=%s",
            account.id,
            phone,
        )
        return Lead.objects.filter(
            organization=account.organization,
            phone=normalized,
        ).first()


def _message_body(payload):
    msg_type = payload.get("type") or "text"
    content = payload.get(msg_type) or {}
    if msg_type == "text":
        return str(content.get("body") or "")
    if isinstance(content, dict):
        return str(content.get("caption") or "")
    return ""


def _model_message_type(msg_type):
    allowed = {
        WhatsAppMessage.MessageType.TEXT,
        WhatsAppMessage.MessageType.IMAGE,
        WhatsAppMessage.MessageType.AUDIO,
        WhatsAppMessage.MessageType.VIDEO,
        WhatsAppMessage.MessageType.DOCUMENT,
    }
    return msg_type if msg_type in allowed else WhatsAppMessage.MessageType.TEXT


def _persist_synced_message(
    *,
    account,
    payload,
    customer_phone,
    direction,
    historical=False,
):
    external_id = str(payload.get("id") or "").strip()
    if not external_id:
        return None
    existing = WhatsAppMessage.objects.filter(external_id=external_id).first()
    if existing:
        return existing

    msg_type = str(payload.get("type") or "text")
    if msg_type in {"edit", "revoke"}:
        detail = payload.get(msg_type) or {}
        original_id = detail.get("original_message_id")
        original = WhatsAppMessage.objects.filter(external_id=original_id).first()
        if original:
            if msg_type == "edit":
                edited = detail.get("message") or {}
                original.body = _message_body(edited)
            else:
                original.body = "[Message deleted from WhatsApp Business app]"
            original.raw_payload = payload
            original.save(update_fields=["body", "raw_payload"])
        return original

    lead = _ensure_lead_for_phone(
        account=account,
        phone=customer_phone,
    )
    business_phone = account.display_phone_number or account.phone_number_id
    if direction == WhatsAppMessage.Direction.OUTBOUND:
        from_number = payload.get("from") or business_phone
        to_number = payload.get("to") or customer_phone
        status = WhatsAppMessage.Status.SENT
    else:
        from_number = payload.get("from") or customer_phone
        to_number = payload.get("to") or business_phone
        status = WhatsAppMessage.Status.RECEIVED

    message = WhatsAppMessage.objects.create(
        organization=account.organization,
        account=account,
        lead=lead,
        direction=direction,
        external_id=external_id,
        from_number=str(from_number or ""),
        to_number=str(to_number or ""),
        body=_message_body(payload),
        message_type=_model_message_type(msg_type),
        media_payload={"coexistence_sync": True, "historical": historical},
        status=status,
        raw_payload=payload,
        # Imported history and Business-App echoes should never inflate unread
        # counts or wake AI. New customer messages still arrive through the
        # normal `messages` webhook and use handle_inbound_message.
        is_read=True,
    )
    from services.channels.realtime import queue_message_publish

    queue_message_publish(message)
    return message


def handle_smb_message_echoes(*, account, value):
    """Mirror staff messages sent from WhatsApp Business App into SHVYA chats."""
    saved = []
    for echo in value.get("message_echoes") or []:
        if not isinstance(echo, dict):
            continue
        customer_phone = echo.get("to") or ""
        message = _persist_synced_message(
            account=account,
            payload=echo,
            customer_phone=customer_phone,
            direction=WhatsAppMessage.Direction.OUTBOUND,
        )
        if message:
            saved.append(message)
    return saved


def handle_history_sync(*, account, value):
    """Import Coexistence history without triggering summaries or AI replies."""
    saved = []
    business_phone = _normalize_phone(
        (value.get("metadata") or {}).get("display_phone_number")
        or account.display_phone_number
    )
    for chunk in value.get("history") or []:
        if not isinstance(chunk, dict):
            continue
        for thread in chunk.get("threads") or []:
            if not isinstance(thread, dict):
                continue
            customer_phone = str(
                thread.get("id") or thread.get("wa_id") or thread.get("chat_id") or ""
            )
            for payload in thread.get("messages") or []:
                if not isinstance(payload, dict):
                    continue
                sender = _normalize_phone(payload.get("from"))
                direction = (
                    WhatsAppMessage.Direction.OUTBOUND
                    if sender and sender == business_phone
                    else WhatsAppMessage.Direction.INBOUND
                )
                message = _persist_synced_message(
                    account=account,
                    payload=payload,
                    customer_phone=customer_phone or payload.get("from") or payload.get("to"),
                    direction=direction,
                    historical=True,
                )
                if message:
                    saved.append(message)
    return saved


def handle_smb_app_state_sync(*, account, value):
    """Apply synced contact names to existing leads without creating address-book leads."""
    updated = 0
    for item in value.get("state_sync") or []:
        if not isinstance(item, dict) or item.get("type") != "contact":
            continue
        contact = item.get("contact") or {}
        phone = _normalize_phone(contact.get("phone_number") or contact.get("wa_id"))
        name = str(contact.get("full_name") or contact.get("first_name") or "").strip()
        if not phone or not name:
            continue
        lead = Lead.objects.filter(
            organization=account.organization,
            phone=phone,
        ).first()
        if lead and (not lead.name or lead.name in {phone, phone.lstrip("+")}):
            lead.name = name
            lead.save(update_fields=["name", "updated_at"])
            updated += 1
    return updated


def process_coexistence_webhook_payload(payload):
    """Process Meta Coexistence-only fields; standard `messages` stay untouched."""
    if not isinstance(payload, dict):
        return
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        waba_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            if not isinstance(change, dict):
                continue
            field = change.get("field")
            if field not in COEXISTENCE_WEBHOOK_FIELDS:
                continue
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            phone_number_id = str(metadata.get("phone_number_id") or "")
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
                continue
            account = accounts.first()
            if not account:
                logger.warning(
                    "Coexistence webhook has no connected API account: field=%s phone=%s waba=%s",
                    field,
                    phone_number_id,
                    waba_id,
                )
                continue
            if field == "smb_message_echoes":
                handle_smb_message_echoes(account=account, value=value)
            elif field == "history":
                handle_history_sync(account=account, value=value)
            elif field == "smb_app_state_sync":
                handle_smb_app_state_sync(account=account, value=value)
