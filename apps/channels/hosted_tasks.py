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
