"""Celery boundaries for Instagram; provider I/O never blocks inbox requests."""
import logging

from celery import shared_task
from django.db import transaction

from .instagram_models import InstagramAccount, InstagramMessage, InstagramOAuthAttempt, InstagramWebhookDelivery

logger = logging.getLogger(__name__)


def _safe_error(exc, account=None):
    from services.channels.instagram_inbox import connection_error
    from services.channels.instagram_service import InstagramAPIError

    detail = connection_error(exc, account) if isinstance(exc, InstagramAPIError) else "Instagram operation failed. Check the worker and connection settings."
    return InstagramAPIError(
        detail,
        status_code=getattr(exc, "status_code", None), code=getattr(exc, "code", None),
        subcode=getattr(exc, "subcode", None), transient=getattr(exc, "transient", False),
    )


def _subscribe(account):
    from services.channels import instagram_service as provider

    result = provider.subscribe_account_webhooks(account)
    # An HTTP 200 without an explicit success must not produce a green inbox.
    if not isinstance(result, dict) or result.get("success") is not True:
        account.webhook_subscribed = False
        account.subscribed_fields = []
        account.save(update_fields=["webhook_subscribed", "subscribed_fields", "updated_at"])
        raise provider.InstagramAPIError("Meta did not confirm the Instagram webhook subscription. Retry inbox setup.")


def _record_account_failure(account, exc):
    from services.channels import instagram_service as provider

    if exc.token_invalid or exc.status_code in (401, 403):
        provider.mark_account_error(account, exc)
    else:
        account.last_error = str(exc)
        account.save(update_fields=["last_error", "updated_at"])


@shared_task(bind=True, max_retries=3, default_retry_delay=20)
def complete_instagram_oauth_task(self, attempt_id):
    from services.channels import instagram_service as provider

    attempt = InstagramOAuthAttempt.objects.filter(pk=attempt_id).first()
    if not attempt:
        return {"status": "missing"}
    if attempt.status == InstagramOAuthAttempt.Status.FAILED:
        return {"status": "failed"}
    account = None
    try:
        account = provider.complete_oauth_attempt(attempt)
        # Reconnects may retain an old subscription flag. Always confirm the
        # subscription for the freshly authorized account/token.
        _subscribe(account)
        provider.sync_account_conversations(account)
    except Exception as original:
        exc = _safe_error(original, account)
        if account and account.status == InstagramAccount.Status.CONNECTED:
            _record_account_failure(account, exc)
            if exc.transient and self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            return {"status": "connected_with_warning", "error": str(exc)}
        # OAuth codes are single-use. Blindly retrying after a token exchange
        # timeout can only obscure the original failure with "code already used".
        provider.fail_oauth_attempt(attempt.pk, exc)
        logger.warning("Instagram authorization failed for attempt %s", attempt.pk)
        return {"status": "failed", "error": str(exc)}
    return {"status": "connected", "account_id": str(account.pk)}


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def sync_instagram_account_task(self, account_id):
    from services.channels import instagram_service as provider

    account = InstagramAccount.objects.filter(pk=account_id, status=InstagramAccount.Status.CONNECTED).first()
    if not account:
        return {"status": "not_connected"}
    try:
        if not account.webhook_subscribed:
            _subscribe(account)
        count = provider.sync_account_conversations(account)
    except Exception as original:
        exc = _safe_error(original, account)
        _record_account_failure(account, exc)
        if exc.transient and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "failed", "error": str(exc)}
    return {"status": "synced", "conversations": count}


@shared_task
def send_instagram_message_task(message_id):
    from services.channels import instagram_service as provider
    from services.channels.instagram_inbox import claim_message

    message = None
    try:
        message = claim_message(message_id)
        if message is None:
            existing = InstagramMessage.objects.filter(pk=message_id).values_list("status", flat=True).first()
            return {"status": existing or "missing", "duplicate_task_ignored": True}
        delivered = provider.send_queued_message(message)
    except Exception as original:
        # No automatic retry after external I/O: a timeout can mean Meta accepted
        # the message. The durable claim also prevents concurrent task delivery.
        exc = _safe_error(original, message.account if message else None)
        provider.fail_message(message_id, exc)
        return {"status": "failed", "error": str(exc)}
    return {"status": delivered.status, "message_id": str(delivered.pk)}


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def process_instagram_webhook_delivery_task(self, delivery_id):
    from services.channels import instagram_service as provider

    delivery = InstagramWebhookDelivery.objects.filter(pk=delivery_id).first()
    if not delivery:
        return {"status": "missing"}
    try:
        with transaction.atomic():
            count = provider.process_webhook_delivery(delivery)
    except Exception as original:
        exc = _safe_error(original)
        provider.fail_webhook_delivery(delivery_id, exc)
        logger.warning("Instagram webhook processing failed for delivery %s", delivery_id)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "failed", "error": str(exc)}
    return {"status": "processed", "events": count}


@shared_task
def refresh_instagram_tokens_task():
    from services.channels import instagram_service as provider

    refreshed = failed = 0
    for account in provider.accounts_due_for_token_refresh().iterator():
        try:
            provider.refresh_account_token(account)
            refreshed += 1
        except Exception as original:
            failed += 1
            _record_account_failure(account, _safe_error(original, account))
            logger.warning("Instagram token refresh failed for account %s", account.pk)
    return {"refreshed": refreshed, "failed": failed}


@shared_task
def disconnect_instagram_account_task(account_id):
    from services.channels import instagram_service as provider

    # Lock across cleanup so an older disconnect cannot clear credentials or
    # unsubscribe an account which has completed a newer reconnect.
    with transaction.atomic():
        account = InstagramAccount.objects.select_for_update().filter(pk=account_id).first()
        if not account:
            return {"status": "missing"}
        if account.status != InstagramAccount.Status.DISCONNECTED:
            return {"status": "superseded"}
        warning = ""
        try:
            provider.unsubscribe_account_webhooks(account)
        except Exception as original:
            warning = str(_safe_error(original, account))
        account.access_token = ""
        account.webhook_subscribed = False
        account.subscribed_fields = []
        account.last_error = warning
        account.save(update_fields=["access_token", "webhook_subscribed", "subscribed_fields", "last_error", "updated_at"])
    return {"status": "disconnected", "warning": warning}
