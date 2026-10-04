"""Celery boundaries for Instagram; provider I/O never blocks inbox requests."""
import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction

from .instagram_models import InstagramAccount, InstagramMessage, InstagramOAuthAttempt, InstagramWebhookDelivery

logger = logging.getLogger(__name__)


def _instagram_retry_delay(*, identifier, retries, retry_after=None):
    if retry_after is not None:
        try:
            base = max(1, min(int(retry_after), 900))
        except (TypeError, ValueError):
            base = 20
    else:
        base = min(300, 15 * (2 ** max(0, int(retries or 0))))
    jitter = (
        sum(ord(character) for character in str(identifier))
        + (int(retries or 0) * 11)
    ) % 7
    return min(907, base + jitter)


def _instagram_admission(account_id):
    from apps.core.fairness import admit_provider_start

    return admit_provider_start(
        provider="instagram",
        account_id=account_id,
        account_limit=settings.INSTAGRAM_ACCOUNT_REQUESTS_PER_MINUTE,
        global_limit=settings.INSTAGRAM_GLOBAL_REQUESTS_PER_MINUTE,
    )


def _safe_error(exc, account=None):
    from services.channels.instagram_inbox import connection_error
    from services.channels.instagram_service import InstagramAPIError

    detail = connection_error(exc, account) if isinstance(exc, InstagramAPIError) else "Instagram operation failed. Check the worker and connection settings."
    return InstagramAPIError(
        detail,
        status_code=getattr(exc, "status_code", None), code=getattr(exc, "code", None),
        subcode=getattr(exc, "subcode", None), transient=getattr(exc, "transient", False),
        retry_after=getattr(exc, "retry_after", None),
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

    with transaction.atomic():
        try:
            current = provider._lock_current_account(account)
        except provider.InstagramOAuthSuperseded:
            return False
        if exc.token_invalid or exc.status_code in (401, 403):
            provider.mark_account_error(current, exc)
        else:
            current.last_error = str(exc)
            current.save(update_fields=["last_error", "updated_at"])
    return True


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
    except provider.InstagramOAuthSuperseded as exc:
        provider.fail_oauth_attempt(attempt.pk, exc)
        return {"status": "superseded"}
    except Exception as original:
        exc = _safe_error(original, account)
        if account and account.status == InstagramAccount.Status.CONNECTED:
            if not _record_account_failure(account, exc):
                return {"status": "superseded"}
            if exc.transient and self.request.retries < self.max_retries:
                raise self.retry(
                    exc=exc,
                    countdown=_instagram_retry_delay(
                        identifier=attempt_id,
                        retries=self.request.retries,
                        retry_after=exc.retry_after,
                    ),
                )
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
    allowed, retry_after, scope = _instagram_admission(account.id)
    if not allowed:
        self.apply_async(
            args=[str(account_id)],
            countdown=_instagram_retry_delay(
                identifier=account_id,
                retries=0,
                retry_after=retry_after,
            ),
        )
        return {
            "status": "deferred",
            "reason": f"{scope}_fairness_limit",
        }
    try:
        if not account.webhook_subscribed or not set(provider.WEBHOOK_FIELDS).issubset(account.subscribed_fields or []):
            _subscribe(account)
        count = provider.sync_account_conversations(account)
    except provider.InstagramOAuthSuperseded:
        return {"status": "superseded"}
    except Exception as original:
        exc = _safe_error(original, account)
        if not _record_account_failure(account, exc):
            return {"status": "superseded"}
        if exc.transient and self.request.retries < self.max_retries:
            raise self.retry(
                exc=exc,
                countdown=_instagram_retry_delay(
                    identifier=account_id,
                    retries=self.request.retries,
                    retry_after=exc.retry_after,
                ),
            )
        return {"status": "failed", "error": str(exc)}
    return {"status": "synced", "conversations": count}


@shared_task(
    bind=True,
    max_retries=3,
    default_retry_delay=20,
    acks_late=True,
    reject_on_worker_lost=True,
)
def generate_instagram_ai_engagement_task(self, message_id):
    """Run one exact Instagram inbound turn through SHVYA AI Brain."""
    from apps.ai_engagement.services.execution_tracker import (
        record_instagram_execution,
    )
    from services.channels.instagram_ai import execute_instagram_ai_engagement

    record_instagram_execution(
        message_id,
        status="processing",
        increment=True,
    )
    result = execute_instagram_ai_engagement(
        task=self,
        message_id=message_id,
    )

    result_status = str(result.get("status") or "").strip().casefold()
    if result_status == "completed":
        record_instagram_execution(message_id, status="completed")
    elif result_status == "queued":
        record_instagram_execution(message_id, status="queued")
    elif result_status in {"skipped", "failed"}:
        reason = str(result.get("reason") or "").strip().casefold()
        record_instagram_execution(
            message_id,
            status=result_status,
            reason=reason,
        )
    return result


@shared_task(bind=True, max_retries=3, default_retry_delay=20)
def send_instagram_message_task(self, message_id):
    from services.channels import instagram_service as provider
    from services.channels.instagram_inbox import claim_message

    account_id = InstagramMessage.objects.filter(pk=message_id).values_list(
        "account_id",
        flat=True,
    ).first()
    if account_id:
        allowed, retry_after, scope = _instagram_admission(account_id)
        if not allowed:
            self.apply_async(
                args=[str(message_id)],
                countdown=_instagram_retry_delay(
                    identifier=message_id,
                    retries=0,
                    retry_after=retry_after,
                ),
            )
            return {
                "status": "deferred",
                "reason": f"{scope}_fairness_limit",
            }

    message = None
    try:
        from apps.channels.services.instagram_automation import outbound_allowed
        candidate = InstagramMessage.objects.select_related("conversation__lead", "account").filter(pk=message_id).first()
        if candidate and not outbound_allowed(candidate):
            return {"status": "deferred", "reason": "instagram_automation_disabled_or_delayed"}
        message = claim_message(message_id)
        if message is None:
            existing = InstagramMessage.objects.filter(pk=message_id).values_list("status", flat=True).first()
            return {"status": existing or "missing", "duplicate_task_ignored": True}
        delivered = provider.send_queued_message(message)
    except Exception as original:
        # Unknown network outcomes are never replayed: Meta may already have
        # accepted the message. An explicit 429 is different—the provider
        # rejected the request before accepting the send, so the durable claim
        # can be cleared and retried safely with provider-aware jitter.
        exc = _safe_error(original, message.account if message else None)
        if (
            isinstance(original, provider.InstagramAPIError)
            and original.status_code == 429
            and self.request.retries < self.max_retries
        ):
            if provider.requeue_explicitly_rejected_message(message_id):
                from apps.core.observability import increment

                increment(
                    "messaging.provider_throttled",
                    labels={"provider": "instagram"},
                )
                raise self.retry(
                    exc=exc,
                    countdown=_instagram_retry_delay(
                        identifier=message_id,
                        retries=self.request.retries,
                        retry_after=exc.retry_after,
                    ),
                )
        provider.fail_message(message_id, exc)
        return {"status": "failed", "error": str(exc)}
    return {"status": delivered.status, "message_id": str(delivered.pk)}


@shared_task(
    bind=True, max_retries=3, default_retry_delay=15,
    acks_late=True, reject_on_worker_lost=True,
)
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
def recover_instagram_webhook_deliveries_task():
    """Recover durable envelopes after lost publication or worker delivery.

    Failed terminal work is left visible for review. Only pending envelopes and
    expired processing leases are retried; row locks and event IDs serialize
    concurrent dispatchers/processors without sending any outbound messages.
    """
    from datetime import timedelta

    from django.db.models import Q
    from django.utils import timezone

    from .instagram_webhook import (
        WEBHOOK_DISPATCH_LEASE_SECONDS, dispatch_instagram_delivery,
    )

    cutoff = timezone.now() - timedelta(seconds=WEBHOOK_DISPATCH_LEASE_SECONDS)
    stale = Q(dispatched_at__lte=cutoff) | Q(
        dispatched_at__isnull=True, received_at__lte=cutoff,
    )
    candidates = list(
        InstagramWebhookDelivery.objects.filter(
            Q(status=InstagramWebhookDelivery.Status.PENDING, received_at__lte=cutoff)
            | (Q(status=InstagramWebhookDelivery.Status.PROCESSING) & stale)
        ).order_by("received_at").values_list("pk", flat=True)[:100]
    )
    recovered = failed = 0
    for delivery_id in candidates:
        try:
            recovered += bool(dispatch_instagram_delivery(delivery_id, recovery_only=True))
        except Exception:
            failed += 1
            logger.warning("Instagram webhook recovery enqueue failed for %s", delivery_id)
    return {"recovered": recovered, "failed": failed}


@shared_task
def refresh_instagram_tokens_task():
    from services.channels import instagram_service as provider

    refreshed = failed = skipped = 0
    for account in provider.accounts_due_for_token_refresh().iterator():
        try:
            if provider.refresh_account_token(account) is None:
                skipped += 1
            else:
                refreshed += 1
        except Exception as original:
            if _record_account_failure(account, _safe_error(original, account)):
                failed += 1
                logger.warning("Instagram token refresh failed for account %s", account.pk)
            else:
                skipped += 1
    return {"refreshed": refreshed, "failed": failed, "skipped": skipped}


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
