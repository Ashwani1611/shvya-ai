import logging

from celery import shared_task

from apps.copilot.models import CopilotScanState
from apps.organizations.models import Organization
from services.copilot_service import get_copilot_config, refresh_organization_flags


logger = logging.getLogger(__name__)


@shared_task
def refresh_copilot_flags_task():
    """Refresh persisted Co-Pilot flags for every enabled organization."""

    refreshed = 0
    failed = 0

    for organization in Organization.objects.filter(is_active=True).iterator():
        if not get_copilot_config(organization)["copilot_enabled"]:
            continue

        try:
            refresh_organization_flags(organization)
            refreshed += 1
        except Exception as exc:  # Celery must continue with the next tenant.
            failed += 1
            logger.exception(
                "Co-Pilot refresh failed for organization %s",
                organization.id,
            )
            CopilotScanState.objects.update_or_create(
                organization=organization,
                defaults={"last_error": str(exc)[:2000]},
            )

    return {"refreshed": refreshed, "failed": failed}
