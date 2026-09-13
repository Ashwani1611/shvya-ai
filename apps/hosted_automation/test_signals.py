from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.hosted_automation.models import HostedAutomationJob
from apps.hosted_automation.signals import (
    dispatch_due_hosted_ai,
    hosted_automation_job_wakeup,
)


class HostedAutomationWakeupTests(SimpleTestCase):
    def test_new_queued_job_respects_full_configured_delay(self):
        instance = SimpleNamespace(
            pk="job-1",
            status=HostedAutomationJob.Status.QUEUED,
            available_at=timezone.now() + timedelta(seconds=60),
        )

        with patch(
            "apps.hosted_automation.signals.transaction.on_commit",
            side_effect=lambda callback: callback(),
        ), patch.object(
            dispatch_due_hosted_ai, "apply_async"
        ) as apply_async:
            hosted_automation_job_wakeup(
                sender=HostedAutomationJob,
                instance=instance,
                created=True,
            )

        apply_async.assert_called_once()
        countdown = apply_async.call_args.kwargs["countdown"]
        # The wakeup must honor available_at itself. It must not subtract the
        # former 15-second processing budget from the configured debounce.
        self.assertGreater(countdown, 55)
        self.assertLessEqual(countdown, 60)

    def test_unrelated_queued_update_does_not_schedule_duplicate_wakeup(self):
        instance = SimpleNamespace(
            pk="job-1",
            status=HostedAutomationJob.Status.QUEUED,
            available_at=timezone.now() + timedelta(seconds=60),
        )

        with patch.object(dispatch_due_hosted_ai, "apply_async") as apply_async:
            hosted_automation_job_wakeup(
                sender=HostedAutomationJob,
                instance=instance,
                created=False,
                update_fields={"result", "updated_at"},
            )

        apply_async.assert_not_called()

    def test_health_requeue_keeps_its_full_pause_deadline(self):
        instance = SimpleNamespace(
            pk="job-1",
            status=HostedAutomationJob.Status.QUEUED,
            available_at=timezone.now() + timedelta(hours=12),
        )

        with patch(
            "apps.hosted_automation.signals.transaction.on_commit",
            side_effect=lambda callback: callback(),
        ), patch.object(
            dispatch_due_hosted_ai, "apply_async"
        ) as apply_async:
            hosted_automation_job_wakeup(
                sender=HostedAutomationJob,
                instance=instance,
                created=False,
                update_fields={"available_at", "updated_at"},
            )

        apply_async.assert_called_once()
        countdown = apply_async.call_args.kwargs["countdown"]
        self.assertGreater(countdown, (12 * 60 * 60) - 5)
        self.assertLessEqual(countdown, 12 * 60 * 60)
