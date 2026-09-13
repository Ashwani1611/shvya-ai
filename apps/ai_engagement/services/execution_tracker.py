"""Durable, content-free AI turn progress and bounded broker recovery."""
from datetime import timedelta
import logging
import re

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

KEY = 'shvya_ai_execution'
logger = logging.getLogger(__name__)


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


def queue_api_engagement(*, lead_id):
    from apps.channels.models import WhatsAppMessage
    from apps.ai_engagement.tasks import generate_ai_engagement_response
    message = WhatsAppMessage.objects.filter(lead_id=lead_id, direction='inbound',
        account__connection_type='api').order_by('-created_at', '-id').first()
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
        latest = message.lead.whatsapp_messages.order_by('-created_at', '-id').first()
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
