"""Durable, content-free AI turn progress and bounded broker recovery."""
from datetime import timedelta
import logging
import re

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

KEY = 'shvya_ai_execution'
ACTIVE_STATUSES = {'queued', 'processing', 'retrying'}
logger = logging.getLogger(__name__)


def active_pinned_source(*, lead=None, lead_id=None, account_id=None):
    """Return the exact manually queued inbound while its AI turn is active.

    Manual inbox lead creation happens after the inbound row already exists.
    Pinning that row prevents later task execution from drifting to another
    message/account while preserving the normal lead-id-only Celery contract.
    """
    from apps.channels.models import WhatsAppMessage

    resolved_lead_id = getattr(lead, "pk", None) or lead_id
    if not resolved_lead_id:
        return None

    query = WhatsAppMessage.objects.filter(
        lead_id=resolved_lead_id,
        direction=WhatsAppMessage.Direction.INBOUND,
        raw_payload__shvya_ai_execution__pinned_source=True,
        raw_payload__shvya_ai_execution__status__in=ACTIVE_STATUSES,
    ).select_related("account")
    if lead is not None:
        query = query.filter(organization_id=lead.organization_id)
    if account_id is not None:
        query = query.filter(account_id=account_id)

    source = query.order_by("-created_at", "-id").first()
    if source is None:
        return None

    execution = (source.raw_payload or {}).get(KEY) or {}
    pinned_account_id = str(execution.get("account_id") or "")
    if pinned_account_id and pinned_account_id != str(source.account_id):
        return None

    # A staff/customer message that arrived later in this same conversation
    # supersedes the manual-create turn. Never answer stale content.
    latest = (
        WhatsAppMessage.objects.filter(
            lead_id=resolved_lead_id,
            organization_id=source.organization_id,
            account_id=source.account_id,
        )
        .order_by("-created_at", "-id")
        .only("id")
        .first()
    )
    if latest is None or latest.pk != source.pk:
        return None
    return source


def _pin_execution_source(*, message, account_id):
    from apps.channels.models import WhatsAppMessage

    with transaction.atomic():
        locked = (
            WhatsAppMessage.objects.select_for_update()
            .filter(
                pk=message.pk,
                lead_id=message.lead_id,
                direction=WhatsAppMessage.Direction.INBOUND,
            )
            .first()
        )
        if locked is None:
            return False
        payload = dict(locked.raw_payload or {})
        previous = dict(payload.get(KEY) or {})
        payload[KEY] = {
            **previous,
            'pinned_source': True,
            'account_id': str(account_id),
        }
        WhatsAppMessage.objects.filter(pk=locked.pk).update(raw_payload=payload)
        message.raw_payload = payload
        return True


def record_execution(message_id, *, status, reason='', increment=False):
    from apps.channels.models import WhatsAppMessage
    reason = reason if re.fullmatch(r'[a-z][a-z0-9_]{0,79}', reason or '') else ''
    with transaction.atomic():
        message = WhatsAppMessage.objects.select_for_update().filter(pk=message_id, direction='inbound').first()
        if message is None:
            return
        payload = dict(message.raw_payload or {})
        previous = payload.get(KEY) or {}
        # Duplicate tasks must not replace the successful turn's final state.
        if (payload.get('shvya_ai_processing') or {}).get('processed') and status in {'processing', 'skipped', 'queued'}:
            return
        payload[KEY] = {**previous, 'status': status, 'reason': reason,
            'attempts': int(previous.get('attempts', 0)) + int(increment),
            'updated_at': timezone.now().isoformat()}
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload=payload)


def claim_execution(message_id, *, stale_after_seconds=180):
    """Atomically claim one inbound AI turn before generation.

    This durable claim complements the Redis generation lock. Duplicate Celery
    deliveries for the same inbound message are rejected while a fresh worker is
    processing it, and stale claims remain recoverable by the existing recovery
    task.
    """
    from apps.channels.models import WhatsAppMessage

    now = timezone.now()
    with transaction.atomic():
        message = (
            WhatsAppMessage.objects.select_for_update()
            .filter(pk=message_id, direction='inbound')
            .first()
        )
        if message is None:
            return False

        payload = dict(message.raw_payload or {})
        if (payload.get('shvya_ai_processing') or {}).get('processed'):
            return False

        previous = payload.get(KEY) or {}
        if previous.get('status') == 'processing':
            stamp = parse_datetime(str(previous.get('updated_at') or ''))
            if stamp and (now - stamp).total_seconds() < stale_after_seconds:
                return False

        payload[KEY] = {
            **previous,
            'status': 'processing',
            'reason': '',
            'attempts': int(previous.get('attempts', 0)) + 1,
            'updated_at': now.isoformat(),
        }
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload=payload)
        return True


def queue_api_engagement(*, lead_id, source_message_id=None, account_id=None):
    from apps.channels.models import WhatsAppMessage
    from apps.ai_engagement.tasks import generate_ai_engagement_response

    if source_message_id is not None:
        message_query = WhatsAppMessage.objects.filter(
            pk=source_message_id,
            lead_id=lead_id,
            direction=WhatsAppMessage.Direction.INBOUND,
            account__connection_type='api',
            account__is_active=True,
        ).select_related("account")
        if account_id is not None:
            message_query = message_query.filter(account_id=account_id)
        message = message_query.first()
        if message is None:
            return None

        media = message.media_payload if isinstance(message.media_payload, dict) else {}
        payload = message.raw_payload if isinstance(message.raw_payload, dict) else {}
        if media.get("historical") is True or payload.get("isHistory") is True:
            record_execution(message.pk, status='skipped', reason='historical_source')
            return None

        latest = (
            WhatsAppMessage.objects.filter(
                lead_id=lead_id,
                organization_id=message.organization_id,
                account_id=message.account_id,
            )
            .order_by("-created_at", "-id")
            .only("id", "direction")
            .first()
        )
        if (
            latest is None
            or latest.pk != message.pk
            or latest.direction != WhatsAppMessage.Direction.INBOUND
        ):
            record_execution(message.pk, status='skipped', reason='conversation_changed')
            return None
        if not _pin_execution_source(message=message, account_id=message.account_id):
            return None
    else:
        message = WhatsAppMessage.objects.filter(
            lead_id=lead_id,
            direction='inbound',
            account__connection_type='api',
        ).order_by('-created_at', '-id').first()

    if message is None:
        return None
    record_execution(message.pk, status='queued')
    # If publication fails, the durable queued marker survives for Beat recovery.
    try:
        return generate_ai_engagement_response.apply_async(args=[str(lead_id)], countdown=0)
    except Exception:
        logger.exception('AI task publication failed; durable turn retained for recovery')
        return None


def recover_api_engagement():
    from apps.channels.models import WhatsAppMessage
    from apps.ai_engagement.tasks import generate_ai_engagement_response
    from apps.channels.tasks import send_whatsapp_message_task
    now = timezone.now()
    cutoff = now - timedelta(hours=1)
    rescued = 0
    # Only tracked turns are recovered: deployment never replays old chats.
    pending = WhatsAppMessage.objects.filter(direction='inbound', account__connection_type='api',
        lead__isnull=False, created_at__gte=cutoff, raw_payload__shvya_ai_execution__status__in=['queued', 'processing', 'retrying']
        ).select_related('lead').order_by('created_at')[:100]
    for message in pending:
        execution = (message.raw_payload or {}).get(KEY) or {}
        if execution.get('status') not in {'queued', 'processing', 'retrying'}:
            continue
        if (message.raw_payload.get('shvya_ai_processing') or {}).get('processed'):
            continue
        stamp = parse_datetime(str(execution.get('updated_at') or ''))
        # Provider generation may legitimately take longer, while an unpublished
        # queued/retrying turn should be recovered quickly.
        delay = 180 if execution.get('status') == 'processing' else 30
        if stamp and (now - stamp).total_seconds() < delay:
            continue
        if int(execution.get('attempts', 0)) >= 5:
            record_execution(message.pk, status='failed', reason='retry_limit_reached')
            continue
        execution = (message.raw_payload or {}).get(KEY) or {}
        latest_query = message.lead.whatsapp_messages
        if execution.get('pinned_source'):
            latest_query = latest_query.filter(account_id=message.account_id)
        latest = latest_query.order_by('-created_at', '-id').first()
        if latest is None or latest.pk != message.pk:
            record_execution(message.pk, status='skipped', reason='conversation_changed')
            continue
        record_execution(message.pk, status='queued', reason='recovered_after_dispatch_delay')
        try:
            generate_ai_engagement_response.apply_async(args=[str(message.lead_id)], countdown=0)
            rescued += 1
        except Exception:
            logger.exception('AI recovery could not publish a retained turn')
    # A successful DB commit followed by failed task publication must not strand
    # its saved reply. Existing sender row locks and status checks protect against
    # duplicate sends, so retry delivery aggressively after a short grace period.
    outgoing = WhatsAppMessage.objects.filter(direction='outbound', status='queued',
        account__connection_type='api', created_at__gte=cutoff,
        created_at__lte=now - timedelta(seconds=10), raw_payload__has_key='shvya_ai')[:100]
    for message in outgoing:
        metadata = (message.raw_payload or {}).get('shvya_ai') or {}
        source = metadata.get('source_inbound_message_id')
        if not source or not WhatsAppMessage.objects.filter(pk=source, organization_id=message.organization_id,
                raw_payload__has_key=KEY).exists():
            continue
        stamp = parse_datetime(str(metadata.get('recovery_dispatched_at') or ''))
        if stamp and (now - stamp).total_seconds() < 15:
            continue
        metadata['recovery_dispatched_at'] = now.isoformat()
        WhatsAppMessage.objects.filter(pk=message.pk).update(raw_payload={**message.raw_payload, 'shvya_ai': metadata})
        try:
            send_whatsapp_message_task.delay(str(message.pk))
            rescued += 1
        except Exception:
            logger.exception('AI recovery could not publish a saved reply')
    return {'requeued': rescued}
