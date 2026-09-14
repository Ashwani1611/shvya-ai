"""Celery jobs for all Instagram network/background operations."""

import logging

from celery import shared_task
from django.db import transaction

from .instagram_models import (
    InstagramAccount,
    InstagramMessage,
    InstagramOAuthAttempt,
    InstagramWebhookDelivery,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=20)
def complete_instagram_oauth_task(self, attempt_id):
    from services.channels.instagram_service import (
        InstagramAPIError,
        complete_oauth_attempt,
        fail_oauth_attempt,
        mark_account_error,
        subscribe_account_webhooks,
        sync_account_conversations,
    )

    attempt = InstagramOAuthAttempt.objects.filter(pk=attempt_id).first()
    if not attempt:
        return {"status": "missing"}
    if attempt.status == InstagramOAuthAttempt.Status.FAILED:
        return {"status": "failed"}

    try:
        account = complete_oauth_attempt(attempt)
        if not account.webhook_subscribed:
            subscribe_account_webhooks(account)
        sync_account_conversations(account)
    except InstagramAPIError as exc:
        account = InstagramAccount.objects.filter(organization=attempt.organization).first()
        if account and attempt.status == InstagramOAuthAttempt.Status.CONNECTED:
            if exc.token_invalid or exc.status_code in (401, 403):
                mark_account_error(account, exc)
            else:
                account.last_error = str(exc)[:2000]
                account.save(update_fields=["last_error", "updated_at"])
            if exc.transient and self.request.retries < self.max_retries:
                raise self.retry(exc=exc)
            return {"status": "connected_with_warning", "error": str(exc)}

        if exc.transient and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        fail_oauth_attempt(attempt.id, exc)
        return {"status": "failed", "error": str(exc)}
    except Exception as exc:
        logger.exception("Instagram OAuth completion failed for %s", attempt_id)
        fail_oauth_attempt(attempt.id, exc)
        raise

    return {"status": "connected", "account_id": str(account.id)}


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def sync_instagram_account_task(self, account_id):
    from services.channels.instagram_service import (
        InstagramAPIError,
        sync_account_conversations,
    )

    account = InstagramAccount.objects.filter(
        pk=account_id,
        status=InstagramAccount.Status.CONNECTED,
    ).first()
    if not account:
        return {"status": "not_connected"}
    try:
        count = sync_account_conversations(account)
        return {"status": "synced", "conversations": count}
    except InstagramAPIError as exc:
        if exc.token_invalid:
            account.status = InstagramAccount.Status.EXPIRED
        elif exc.status_code in (401, 403):
            account.status = InstagramAccount.Status.REVOKED
        account.last_error = str(exc)[:2000]
        account.save(update_fields=["status", "last_error", "updated_at"])
        if exc.transient and self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "failed", "error": str(exc)}


@shared_task
def send_instagram_message_task(message_id):
    """Send once rather than blindly retrying an ambiguous Meta timeout."""
    from services.channels.instagram_service import (
        InstagramAPIError,
        fail_message,
        send_queued_message,
    )

    message = InstagramMessage.objects.filter(pk=message_id).first()
    if not message:
        return {"status": "missing"}
    try:
        delivered = send_queued_message(message)
    except InstagramAPIError as exc:
        fail_message(message.id, exc)
        return {"status": "failed", "error": str(exc)}
    return {"status": delivered.status, "message_id": str(delivered.id)}


@shared_task(bind=True, max_retries=3, default_retry_delay=15)
def process_instagram_webhook_delivery_task(self, delivery_id):
    from services.channels.instagram_service import (
        fail_webhook_delivery,
        process_webhook_delivery,
    )

    delivery = InstagramWebhookDelivery.objects.filter(pk=delivery_id).first()
    if not delivery:
        return {"status": "missing"}
    try:
        with transaction.atomic():
            count = process_webhook_delivery(delivery)
    except Exception as exc:
        logger.exception("Instagram webhook processing failed for %s", delivery_id)
        fail_webhook_delivery(delivery_id, exc)
        if self.request.retries < self.max_retries:
            raise self.retry(exc=exc)
        return {"status": "failed", "error": str(exc)}
    return {"status": "processed", "events": count}


@shared_task
def refresh_instagram_tokens_task():
    from services.channels.instagram_service import (
        InstagramAPIError,
        accounts_due_for_token_refresh,
        mark_account_error,
        refresh_account_token,
    )

    refreshed = 0
    failed = 0
    for account in accounts_due_for_token_refresh().iterator():
        try:
            refresh_account_token(account)
            refreshed += 1
        except InstagramAPIError as exc:
            failed += 1
            if exc.token_invalid or exc.status_code in (401, 403):
                mark_account_error(account, exc)
            else:
                account.last_error = str(exc)[:2000]
                account.save(update_fields=["last_error", "updated_at"])
            logger.warning("Instagram token refresh failed for %s: %s", account.id, exc)
    return {"refreshed": refreshed, "failed": failed}


@shared_task
def disconnect_instagram_account_task(account_id):
    from services.channels.instagram_service import (
        InstagramAPIError,
        unsubscribe_account_webhooks,
    )

    account = InstagramAccount.objects.filter(pk=account_id).first()
    if not account:
        return {"status": "missing"}
    warning = ""
    try:
        unsubscribe_account_webhooks(account)
    except InstagramAPIError as exc:
        warning = str(exc)
        logger.warning("Could not unsubscribe Instagram account %s: %s", account.id, exc)

    account.status = InstagramAccount.Status.DISCONNECTED
    account.access_token = ""
    account.webhook_subscribed = False
    account.subscribed_fields = []
    account.last_error = warning[:2000]
    account.save(
        update_fields=[
            "status",
            "access_token",
            "webhook_subscribed",
            "subscribed_fields",
            "last_error",
            "updated_at",
        ]
    )
    return {"status": "disconnected", "warning": warning}
