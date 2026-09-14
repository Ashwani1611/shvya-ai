"""Strict Account Health enforcement for Hosted WhatsApp.

The hosted gateway can observe outbound messages that were sent directly from
WhatsApp / WhatsApp Business as well as messages sent by SHVYA.  The original
health counter only advanced on SHVYA transport sends, which meant automation
could continue after the real connected number had already crossed the 250
message protection threshold.

This module reconciles the durable health row against successfully-sent,
non-history outbound WhatsAppMessage rows and reserves automation slots before
provider delivery so concurrent AI/follow-up work cannot overshoot the limit.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.channels.models import WhatsAppMessage
from apps.hosted_automation.models import HostedAccountHealth


HOSTED_CONNECTION_TYPE = "hosted"
RECOMMENDED_MESSAGING_LIMIT = 250
HEALTH_COOLDOWN = timedelta(hours=12)

_SENT_STATUSES = (
    WhatsAppMessage.Status.SENT,
    WhatsAppMessage.Status.DELIVERED,
    WhatsAppMessage.Status.READ,
)


def _window_start(account, now):
    return (
        getattr(account, "connected_at", None)
        or getattr(account, "created_at", None)
        or now
    )


def _sent_messages(*, account):
    # History sync is observational data and must never consume today's health
    # budget.  Realtime outbound rows include both SHVYA sends and messages sent
    # directly from the linked WhatsApp / WhatsApp Business app.
    return (
        WhatsAppMessage.objects.filter(
            organization_id=account.organization_id,
            account_id=account.id,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            status__in=_SENT_STATUSES,
        )
        .exclude(raw_payload__isHistory=True)
    )


def _locked_health(*, account, now):
    if account.connection_type != HOSTED_CONNECTION_TYPE:
        return None
    health, _created = HostedAccountHealth.objects.get_or_create(
        account=account,
        defaults={"window_started_at": _window_start(account, now)},
    )
    return HostedAccountHealth.objects.select_for_update().get(pk=health.pk)


def _reconcile_locked(*, account, health, now):
    if health.paused_until and health.paused_until <= now:
        health.paused_until = None
        health.window_messages_sent = 0
        health.window_started_at = now

    if not health.window_started_at:
        health.window_started_at = _window_start(account, now)

    sent = _sent_messages(account=account)
    actual_total = sent.count()
    actual_window = sent.filter(created_at__gte=health.window_started_at).count()

    # Never reduce durable counters during normal reconciliation.  The window
    # can temporarily be ahead of the database because an automation slot is
    # reserved immediately before the provider call.
    health.total_messages_sent = max(health.total_messages_sent, actual_total)
    health.window_messages_sent = max(health.window_messages_sent, actual_window)

    if health.enabled and health.window_messages_sent >= RECOMMENDED_MESSAGING_LIMIT:
        if not (health.paused_until and health.paused_until > now):
            health.paused_until = now + HEALTH_COOLDOWN

    health.save()
    return health


@transaction.atomic
def sync_hosted_health_from_messages(*, account):
    """Reconcile Account Health with the real hosted-number outbound activity."""
    now = timezone.now()
    health = _locked_health(account=account, now=now)
    if health is None:
        return None
    return _reconcile_locked(account=account, health=health, now=now)


def hosted_health_pause_until(*, account):
    """Return an active health pause after first reconciling real sends."""
    health = sync_hosted_health_from_messages(account=account)
    if not health or not health.enabled:
        return None
    if health.paused_until and health.paused_until > timezone.now():
        return health.paused_until
    return None


@transaction.atomic
def reserve_hosted_automation_send(*, account):
    """Atomically reserve one automation send slot.

    The reservation happens before the provider call.  This closes the race
    where an AI reply and an auto-follow-up could both observe 249 and both send.
    The 250th automated message is allowed; subsequent automation is paused for
    the configured cooldown.
    """
    now = timezone.now()
    health = _locked_health(account=account, now=now)
    if health is None:
        return {"reserved": False, "blocked_until": None}

    health = _reconcile_locked(account=account, health=health, now=now)
    if not health.enabled:
        return {"reserved": False, "blocked_until": None}

    if health.paused_until and health.paused_until > now:
        return {"reserved": False, "blocked_until": health.paused_until}

    if health.window_messages_sent >= RECOMMENDED_MESSAGING_LIMIT:
        health.paused_until = now + HEALTH_COOLDOWN
        health.save(update_fields=["paused_until", "updated_at"])
        return {"reserved": False, "blocked_until": health.paused_until}

    health.window_messages_sent += 1
    if health.window_messages_sent >= RECOMMENDED_MESSAGING_LIMIT:
        # Reserve the final allowed slot and close the gate immediately so no
        # concurrent automation can become message 251.
        health.paused_until = now + HEALTH_COOLDOWN
    health.save(update_fields=["window_messages_sent", "paused_until", "updated_at"])
    return {"reserved": True, "blocked_until": None}


@transaction.atomic
def release_hosted_automation_reservation(*, account):
    """Release a reserved slot when the provider did not accept the message."""
    now = timezone.now()
    health = _locked_health(account=account, now=now)
    if health is None:
        return None

    # Recompute successful sends first.  Then remove exactly one outstanding
    # reservation without dropping below the real sent-message count.
    sent = _sent_messages(account=account)
    actual_window = 0
    if health.window_started_at:
        actual_window = sent.filter(created_at__gte=health.window_started_at).count()

    if health.window_messages_sent > actual_window:
        health.window_messages_sent -= 1
    health.window_messages_sent = max(health.window_messages_sent, actual_window)

    if health.window_messages_sent < RECOMMENDED_MESSAGING_LIMIT:
        health.paused_until = None
    elif health.enabled and not (health.paused_until and health.paused_until > now):
        health.paused_until = now + HEALTH_COOLDOWN

    health.save(update_fields=["window_messages_sent", "paused_until", "updated_at"])
    return health


@transaction.atomic
def finalize_hosted_send(*, account, message=None):
    """Reconcile counters after one successfully accepted provider send."""
    now = timezone.now()
    health = _locked_health(account=account, now=now)
    if health is None:
        return None

    health = _reconcile_locked(account=account, health=health, now=now)

    payload = message.raw_payload if message and isinstance(message.raw_payload, dict) else {}
    followup = payload.get("shvya_auto_followup") or {}
    content_hash = str(followup.get("content_hash") or "")
    if content_hash:
        health.last_followup_sent_at = now
        health.last_followup_content_hash = content_hash
        health.save(
            update_fields=[
                "last_followup_sent_at",
                "last_followup_content_hash",
                "updated_at",
            ]
        )
    return health
