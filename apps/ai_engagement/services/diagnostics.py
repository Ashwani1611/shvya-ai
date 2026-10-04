"""Inspect a conversation without calling providers or modifying CRM state."""

from django.conf import settings
from apps.ai_engagement.services.playbook import qualification_questions

from apps.ai_engagement.models import AICreditWallet, OrgInfo
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.channels.models import WhatsAppMessage
from apps.hosted_automation.models import HostedAccountHealth, HostedAutomationJob
from django.utils import timezone
from services.channels.hosted_whatsapp_service import get_session_settings


# Hosted result payloads can also contain provider exceptions. Only fixed codes
# authored by the worker/permission checks are appropriate for this dashboard.
_HOSTED_REASON_CODES = frozenset({
    "account_health_pause", "ai_auto_reply_disabled", "conversation_changed_before_generation",
    "conversation_changed_before_send", "duplicate_ai_response", "empty_engagement_message",
    "generated_message_missing", "generated_message_not_queued", "job_lease_replaced",
    "lead_has_no_phone_number", "lead_missing", "lead_not_found", "lead_organization_mismatch",
    "lead_ai_disabled", "message_already_processed", "no_connected_whatsapp_account",
    "no_engagement", "no_whatsapp_account", "nonconversational_inbound", "organization_ai_disabled",
    "organization_mismatch", "pipeline_ai_disabled", "pipeline_whatsapp_account_mismatch",
    "pipeline_whatsapp_number_missing", "provider_ack_pending", "provider_transient",
    "retry_limit_exceeded", "retry_scheduled", "session_reconnecting", "source_message_is_history",
    "source_message_missing", "source_message_missing_timestamp", "source_message_not_inbound",
    "stage_ai_disabled", "superseded_by_newer_lead_message", "unsupported_whatsapp_connection_type",
    "whatsapp_24h_window_expired", "whatsapp_account_inactive", "whatsapp_account_not_connected",
    "whatsapp_account_number_missing", "whatsapp_account_organization_mismatch",
})


def _hosted_job_snapshot(job):
    """Expose durable progress without result contents or private errors."""
    result = job.result if isinstance(job.result, dict) else {}
    reason = result.get("reason")
    if job.status == HostedAutomationJob.Status.QUEUED:
        reason = result.get("defer_reason") or reason
    return {
        "id": str(job.id),
        "source_message_id": str(job.source_message_id),
        "status": job.status,
        "reason": reason if isinstance(reason, str) and reason in _HOSTED_REASON_CODES else "",
        "attempts": job.attempts,
        "available_at": job.available_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


class _ReadOnlyOrgInfoService:
    """Read persisted AI controls without creating default CRM state."""

    def get_or_create(self, *, organization):
        return (
            OrgInfo.objects.filter(
                organization=organization
            ).first()
            or OrgInfo(organization=organization)
        )


def diagnose_engagement(*, lead):
    blockers = []
    report = {"lead_id": str(lead.id), "blockers": blockers}
    if not getattr(settings, "OPENAI_API_KEY", "").strip():
        blockers.append("openai_api_key_missing")
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        blockers.append("celery_eager_mode_use_production_settings")
    wallet = AICreditWallet.objects.filter(organization=lead.organization).first()
    report["available_ai_credits"] = wallet.available_credits if wallet else 0
    if wallet and wallet.is_blocked:
        blockers.append("organization_ai_credits_blocked")
    elif not wallet or wallet.available_credits <= 0:
        blockers.append("organization_ai_credits_empty")
    messages = (
        lead.whatsapp_messages.filter(
            organization=lead.organization,
            account__organization=lead.organization,
        )
        .select_related(
            "account",
            "account__organization",
        )
        .defer("account__access_token")
    )
    latest = (
        messages.order_by("-created_at", "-id")
        .first()
    )
    if latest is None:
        blockers.append("no_whatsapp_messages_check_webhook_and_lead_attachment")
        return report
    report["latest_message"] = {
        "id": str(latest.id),
        "direction": latest.direction,
        "status": latest.status,
        "created_at": latest.created_at.isoformat(),
    }
    account = latest.account
    inbound_messages = messages.filter(direction=WhatsAppMessage.Direction.INBOUND)
    if account.connection_type == "hosted":
        inbound_messages = inbound_messages.filter(account=account)
    inbound = (
        inbound_messages
        .order_by("-created_at", "-id")
        .first()
    )
    permission = AIPermissionService(
        org_info_service=_ReadOnlyOrgInfoService()
    ).evaluate(
        organization=lead.organization,
        lead=lead,
        latest_inbound=inbound,
        account=account if account.connection_type == "hosted" else None,
    )
    if not permission.allowed:
        blockers.append(permission.reason)
    if inbound:
        inbound_payload = inbound.raw_payload if isinstance(inbound.raw_payload, dict) else {}
        report["latest_inbound_message"] = {
            "id": str(inbound.id),
            "created_at": inbound.created_at.isoformat(),
            "is_history": bool(inbound_payload.get("isHistory")),
        }
        execution = (inbound.raw_payload or {}).get("shvya_ai_execution") or {}
        report["execution"] = {key: execution.get(key) for key in ("status", "reason", "attempts", "updated_at")}
        report["inbound_processed"] = bool(((inbound.raw_payload or {}).get("shvya_ai_processing") or {}).get("processed"))
    from apps.ai_engagement.services.qualification_state import state_for_lead, requirements_for_lead
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
    from apps.ai_engagement.models import OrgInfo
    info = OrgInfo.objects.filter(organization=lead.organization).first()
    requirements = requirements_for_lead(lead, compile_qualification_requirements(qualification_questions(info.ai_playbook if info else ""))["requirements"])
    qualification = state_for_lead(lead, requirements=requirements)
    report["qualification"] = {key: qualification.get(key) for key in
        ("qualification_status", "conversation_mode", "current_requirement_id", "last_asked_requirement_id")}
    report["answered_count"] = len(qualification.get("answered_requirement_ids") or [])
    report["account_id"] = str(account.id)
    report["connection_type"] = account.connection_type
    if not get_session_settings(account=account).get("ai_auto_reply"):
        blockers.append("ai_auto_reply_disabled")
    if latest.direction == WhatsAppMessage.Direction.INBOUND:
        from apps.ai_engagement.tasks import _whatsapp_send_eligible

        eligible, reason = _whatsapp_send_eligible(
            lead=lead, inbound_message=latest, account=account
        )
        if not eligible:
            blockers.append(reason)
    elif latest.status == WhatsAppMessage.Status.FAILED:
        blockers.append("latest_outbound_delivery_failed_check_worker_logs")
    elif latest.status == WhatsAppMessage.Status.QUEUED:
        blockers.append("outbound_queued_check_worker")
    else:
        blockers.append("latest_message_not_inbound")
    if account.connection_type == "hosted":
        health = HostedAccountHealth.objects.filter(
            account=account, account__organization=lead.organization
        ).first()
        pause = health.paused_until if health and health.enabled else None
        if pause and pause <= timezone.now():
            pause = None
        if pause:
            blockers.append("hosted_account_health_pause")
            report["paused_until"] = pause.isoformat()
        job = (
            HostedAutomationJob.objects.filter(
                organization=lead.organization,
                account=account,
                account__organization=lead.organization,
                lead=lead,
                kind=HostedAutomationJob.Kind.AI_ENGAGEMENT,
                source_message=inbound,
                source_message__organization=lead.organization,
                source_message__account=account,
                source_message__lead=lead,
                source_message__direction=WhatsAppMessage.Direction.INBOUND,
            )
            .first()
        ) if inbound else None
        if job:
            snapshot = _hosted_job_snapshot(job)
            report["hosted_job"] = snapshot
            # Hosted owns a durable job instead of the API execution marker.
            report["execution"] = {
                key: snapshot[key]
                for key in ("status", "reason", "attempts", "updated_at", "source_message_id")
            }
        else:
            is_history = bool(inbound and report["latest_inbound_message"]["is_history"])
            report["execution"] = {
                "status": "history_import" if is_history else "not_queued" if inbound else "not_started",
                "reason": "source_message_is_history" if is_history else "",
                "source_message_id": str(inbound.id) if inbound else None,
            }
            blockers.append(
                "source_message_is_history" if is_history
                else "no_hosted_ai_job_check_live_inbound_and_lead_mapping"
            )
    return report
