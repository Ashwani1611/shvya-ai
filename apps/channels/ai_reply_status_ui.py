"""Organization-scoped operational status; never exposes raw logs or errors."""
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET
from apps.crm.decorators import crm_login_required
from apps.crm.models import Lead
from apps.ai_engagement.services.diagnostics import diagnose_engagement

LABELS = {
    'queued': 'Waiting for the AI worker', 'processing': 'Preparing a reply',
    'retrying': 'Retrying a temporary failure', 'failed': 'Reply could not be completed',
    'completed': 'AI processing completed', 'skipped': 'Reply was skipped',
    'not_queued': 'No Hosted AI reply queued for the latest incoming message',
    'history_import': 'Historical import; no AI reply queued',
    'organization_ai_disabled': 'AI is disabled for this organization.',
    'pipeline_ai_disabled': 'AI is disabled for this pipeline.',
    'stage_ai_disabled': 'AI is disabled for this stage.',
    'lead_ai_disabled': 'AI is disabled for this lead.',
    'pipeline_whatsapp_account_mismatch': 'The incoming number does not match the pipeline number.',
    'pipeline_whatsapp_number_missing': 'This pipeline has no linked WhatsApp number.',
    'whatsapp_account_organization_mismatch': 'The incoming WhatsApp account does not belong to this organization.',
    'whatsapp_account_inactive': 'The incoming WhatsApp account is inactive.',
    'whatsapp_account_not_connected': 'The incoming WhatsApp account is not connected.',
    'unsupported_whatsapp_connection_type': 'This WhatsApp connection type cannot run AI replies.',
    'conversation_whatsapp_account_missing': 'The active WhatsApp conversation could not be resolved.',
    'ai_auto_reply_disabled': 'Automatic replies are disabled for this account.',
    'hosted_account_health_pause': 'Account health protection has paused automation.',
    'openai_api_key_missing': 'The AI provider is not configured.',
    'organization_ai_credits_empty': 'No AI credits are available.',
    'organization_ai_credits_blocked': 'AI credits are blocked for this organization.',
    'engagement_generation_failed': 'The AI provider or response validation rejected this reply.',
    'finalization_failed': 'The response could not be saved for delivery.',
    'latest_message_not_inbound': 'The latest message is outgoing; waiting for the customer.',
    'outbound_queued_check_worker': 'The reply is saved and waiting for delivery.',
    'latest_outbound_delivery_failed_check_worker_logs': 'WhatsApp rejected or could not deliver the saved reply.',
    'no_hosted_ai_job_check_live_inbound_and_lead_mapping': 'No Hosted AI job is recorded for this incoming message. Check live-message and lead mapping.',
    'superseded_by_newer_lead_message': 'A newer incoming message replaced this reply.',
    'source_message_is_history': 'This incoming message was imported from WhatsApp history. Historical imports do not start AI replies.',
    'conversation_changed_before_generation': 'The conversation changed before AI generation.',
    'conversation_changed_before_send': 'The conversation changed before delivery.',
    'duplicate_ai_response': 'A reply already exists for this incoming message.',
    'message_already_processed': 'This incoming message was already processed.',
    'no_engagement': 'AI processed this message and determined that no reply was needed.',
    'account_health_pause': 'Account health protection has paused automation.',
    'retry_scheduled': 'A temporary worker failure is waiting for another attempt.',
    'retry_limit_exceeded': 'The worker exhausted its retry limit.',
    'provider_transient': 'The AI provider is temporarily unavailable.',
    'provider_ack_pending': 'WhatsApp delivery confirmation is pending.',
    'session_reconnecting': 'The WhatsApp session is reconnecting.',
}


@crm_login_required
@require_GET
def ai_reply_status(request, lead_id):
    lead = get_object_or_404(Lead.objects.select_related('organization', 'pipeline', 'stage'),
        pk=lead_id, organization=request.crm_user.organization)
    report = diagnose_engagement(lead=lead)
    execution = report.get('execution') or {}
    status = execution.get('status') or 'not_started'
    codes = list(report.get('blockers') or [])
    if execution.get('reason'):
        codes.append(execution['reason'])
    return render(request, 'channels/ai_reply_status.html', {
        'lead': lead, 'report': report, 'status_label': LABELS.get(status, 'No AI attempt recorded for the latest incoming message'),
        'reasons': [LABELS.get(code, 'Automation status: ' + code.replace('_', ' ')) for code in dict.fromkeys(codes)],
    })
