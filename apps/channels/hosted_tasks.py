import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=20)
def initialize_hosted_session_task(self, account_id):
    from apps.channels.models import WhatsAppAccount
    from apps.channels.providers.whatsapp_web import (
        WhatsAppWebClient,
        WhatsAppWebGatewayError,
    )

    account = WhatsAppAccount.objects.filter(
        id=account_id,
        connection_type="hosted",
        is_active=True,
    ).first()
    if not account:
        return {"status": "skipped", "reason": "account_not_found"}

    try:
        result = WhatsAppWebClient().create_session(
            session_id=account.id,
            phone_number=account.display_phone_number or account.phone_number_id,
        )
    except WhatsAppWebGatewayError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise self.retry(exc=exc)
        account.status = WhatsAppAccount.Status.FAILED
        account.save(update_fields=["status", "updated_at"])
        return {"status": "failed", "error": str(exc)}

    account.status = WhatsAppAccount.Status.PENDING
    account.save(update_fields=["status", "updated_at"])
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=10)
def sync_hosted_history_task(self, account_id):
    """Reconcile a hosted session with the gateway and backfill recent history."""
    from apps.channels.models import WhatsAppAccount
    from apps.channels.providers.whatsapp_web import (
        WhatsAppWebClient,
        WhatsAppWebGatewayError,
    )
    from services.channels.hosted_whatsapp_service import handle_gateway_event

    account = WhatsAppAccount.objects.filter(
        id=account_id,
        connection_type="hosted",
        is_active=True,
    ).first()
    if not account:
        return {"status": "skipped", "reason": "account_not_found"}

    client = WhatsAppWebClient()
    try:
        session = client.get_session(session_id=account.id)
    except WhatsAppWebGatewayError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise self.retry(exc=exc)
        return {"status": "failed", "error": str(exc)}

    gateway_status = str(session.get("status") or "").lower()
    phone_number = session.get("phoneNumber") or account.display_phone_number

    if gateway_status == "running":
        # Callbacks are the normal source of truth, but reconciling here makes
        # the UI self-healing if a previous callback was lost or redirected.
        handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": "ready",
                "phoneNumber": phone_number,
            }
        )
        try:
            result = client.sync_history(session_id=account.id)
        except WhatsAppWebGatewayError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise self.retry(exc=exc)
            return {"status": "failed", "error": str(exc)}
        return {"status": "synced", **result}

    if gateway_status in {"initializing", "connecting", "authenticated", "syncing"}:
        raise self.retry(countdown=10)

    if gateway_status in {"failed", "disconnected"}:
        handle_gateway_event(
            payload={
                "sessionId": str(account.id),
                "event": gateway_status,
            }
        )

    return {"status": "skipped", "gateway_status": gateway_status}


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def refresh_hosted_qr_task(self, account_id):
    from apps.channels.models import WhatsAppAccount
    from apps.channels.providers.whatsapp_web import (
        WhatsAppWebClient,
        WhatsAppWebGatewayError,
    )

    account = WhatsAppAccount.objects.filter(
        id=account_id,
        connection_type="hosted",
        is_active=True,
    ).first()
    if not account:
        return {"status": "skipped", "reason": "account_not_found"}

    try:
        result = WhatsAppWebClient().refresh_qr(session_id=account.id)
    except WhatsAppWebGatewayError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise self.retry(exc=exc)
        return {"status": "failed", "error": str(exc)}

    account.status = WhatsAppAccount.Status.PENDING
    account.save(update_fields=["status", "updated_at"])
    return result


@shared_task(bind=True, max_retries=2, default_retry_delay=10)
def logout_hosted_session_task(self, account_id):
    from apps.channels.models import WhatsAppAccount
    from apps.channels.providers.whatsapp_web import (
        WhatsAppWebClient,
        WhatsAppWebGatewayError,
    )

    account = WhatsAppAccount.objects.filter(
        id=account_id,
        connection_type="hosted",
        is_active=True,
    ).first()
    if not account:
        return {"status": "skipped", "reason": "account_not_found"}

    try:
        result = WhatsAppWebClient().logout(session_id=account.id)
    except WhatsAppWebGatewayError as exc:
        if exc.status_code is None or exc.status_code >= 500:
            raise self.retry(exc=exc)
        result = {"status": "failed", "error": str(exc)}

    account.status = WhatsAppAccount.Status.DISCONNECTED
    account.save(update_fields=["status", "updated_at"])
    return result
