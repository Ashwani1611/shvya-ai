import logging

from celery import shared_task

from services.channels.hosted_automation_service import (
    dispatch_one_api_due_state,
    dispatch_one_hosted_ai_job,
    dispatch_one_hosted_due_state,
)


logger = logging.getLogger(__name__)


@shared_task(name="apps.followups.tasks.dispatch_auto_followups_task")
def dispatch_auto_followups_task():
    """Run the 20-second scheduler.

    Hosted Account priority is strict: a due AI Engagement job is dispatched
    before any Hosted Auto Follow-up. Meta WhatsApp API has its own provider
    lane and continues independently so Hosted traffic cannot starve it.
    """
    ai_result = dispatch_one_hosted_ai_job()
    if ai_result.get("status") == "dispatched":
        hosted_result = {"status": "waiting_for_ai_priority"}
    else:
        hosted_result = dispatch_one_hosted_due_state()

    api_result = dispatch_one_api_due_state()
    logger.debug(
        "Auto Follow-ups dispatcher results: ai=%s hosted=%s api=%s",
        ai_result,
        hosted_result,
        api_result,
    )
    return {
        "hosted_ai": ai_result,
        "hosted_followup": hosted_result,
        "whatsapp_api": api_result,
    }
