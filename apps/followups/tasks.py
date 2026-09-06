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
    """Run the 20-second automation scheduler with Hosted AI first."""
    ai_result = dispatch_one_hosted_ai_job()
    if ai_result.get("status") == "dispatched":
        logger.debug("Hosted AI dispatcher result: %s", ai_result)
        return {"lane": "hosted_ai", **ai_result}

    hosted_result = dispatch_one_hosted_due_state()
    if hosted_result.get("status") in {"processed", "deferred"}:
        logger.debug("Hosted follow-up dispatcher result: %s", hosted_result)
        return {"lane": "hosted_followup", **hosted_result}

    api_result = dispatch_one_api_due_state()
    logger.debug(
        "Auto Follow-ups dispatcher results: ai=%s hosted=%s api=%s",
        ai_result,
        hosted_result,
        api_result,
    )
    return {"lane": "whatsapp_api", **api_result}
