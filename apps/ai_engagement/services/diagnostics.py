"""Inspect a conversation without calling providers or modifying CRM state."""

from django.conf import settings

from apps.ai_engagement.models import AICreditWallet
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.channels.models import WhatsAppMessage
from apps.hosted_automation.models import HostedAccountHealth, HostedAutomationJob
from django.utils import timezone
from services.channels.hosted_whatsapp_service import get_session_settings


def diagnose_engagement(*, lead):
    blockers = []
    report = {"lead_id": str(lead.id), "blockers": blockers}
    if not getattr(settings, "OPENAI_API_KEY", "").strip():
        blockers.append("openai_api_key_missing")
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        blockers.append("celery_eager_mode_use_production_settings")
    permission = AIPermissionService().evaluate(
        organization=lead.organization, lead=lead
    )
    if not permission.allowed:
        blockers.append(permission.reason)
    wallet = AICreditWallet.objects.filter(organization=lead.organization).first()
    report["available_ai_credits"] = wallet.available_credits if wallet else 0
    if wallet and wallet.is_blocked:
        blockers.append("organization_ai_credits_blocked")
    elif not wallet or wallet.available_credits <= 0:
        blockers.append("organization_ai_credits_empty")
    latest = (
        lead.whatsapp_messages.filter(organization=lead.organization)
        .select_related("account__organization")
        .order_by("-created_at", "-id")
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
    inbound = lead.whatsapp_messages.filter(organization=lead.organization, direction="inbound").order_by("-created_at", "-id").first()
    if inbound:
        execution = (inbound.raw_payload or {}).get("shvya_ai_execution") or {}
        report["execution"] = {key: execution.get(key) for key in ("status", "reason", "attempts", "updated_at")}
        report["inbound_processed"] = bool(((inbound.raw_payload or {}).get("shvya_ai_processing") or {}).get("processed"))
    from apps.ai_engagement.services.qualification_state import state_for_lead, requirements_for_lead
    from apps.ai_engagement.services.organization_profile import compile_qualification_requirements
    from apps.ai_engagement.models import OrgInfo
    info = OrgInfo.objects.filter(organization=lead.organization).first()
    requirements = requirements_for_lead(lead, compile_qualification_requirements(info.qualification_requirements if info else "")["requirements"])
    qualification = state_for_lead(lead, requirements=requirements)
    report["qualification"] = {key: qualification.get(key) for key in
        ("qualification_status", "conversation_mode", "current_requirement_id", "last_asked_requirement_id")}
    report["answered_count"] = len(qualification.get("answered_requirement_ids") or [])
    account = latest.account
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
                organization=lead.organization, lead=lead, account=account
            )
            .order_by("-created_at")
            .first()
        )
        if job:
            # Do not expose job.error: provider errors can contain credentials.
            report["hosted_job"] = {
                "id": str(job.id),
                "status": job.status,
                "available_at": job.available_at.isoformat(),
            }
        else:
            blockers.append("no_hosted_ai_job_check_live_inbound_and_lead_mapping")
    return report
