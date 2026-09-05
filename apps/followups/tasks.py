import logging

from celery import shared_task

from services.followup_service import dispatch_one_due_state


logger = logging.getLogger(__name__)


@shared_task(name="apps.followups.tasks.dispatch_auto_followups_task")
def dispatch_auto_followups_task():
    """Drain one due lead at a time; Beat invokes this every 10 seconds."""
    result = dispatch_one_due_state()
    logger.debug("Auto Follow-ups dispatcher result: %s", result)
    return result
