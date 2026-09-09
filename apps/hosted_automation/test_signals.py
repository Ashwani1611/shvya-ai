from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.hosted_automation.models import HostedAutomationJob
from apps.hosted_automation.signals import (
    HOSTED_AI_PROCESSING_BUDGET_SECONDS,
    dispatch_due_hosted_ai,
    hosted_automation_job_wakeup,
)


class HostedAutomationWakeupTests(SimpleTestCase):
    def test_new_queued_job_reserves_processing_time_before_60_second_target(self):
        instance = SimpleNamespace(
            pk="job-1",
            status=HostedAutomationJob.Status.QUEUED,
            available_at=timezone.now() + timedelta(seconds=60),
        )

        with patch(
            "apps.hosted_automation.signals.transaction.on_commit",
            side_effect=lambda callback: callback(),
        ), patch(
            "apps.hosted_automation.signals.HostedAutomationJob.objects.filter"
        ) as filter_jobs, patch.object(
            dispatch_due_hosted_ai, "apply_async"
        ) as apply_async:
            hosted_automation_job_wakeup(
                sender=HostedAutomationJob,
                instance=instance,
                created=True,
            )

        filter_jobs.assert_called_once_with(
            pk="job-1",
            status=HostedAutomationJob.Status.QUEUED,
        )
        filter_jobs.return_value.update.assert_called_once()
        persisted_due = filter_jobs.return_value.update.call_args.kwargs["available_at"]
        self.assertEqual(persisted_due, instance.available_at)

        apply_async.assert_called_once()
        countdown = apply_async.call_args.kwargs["countdown"]
        expected = 60 - HOSTED_AI_PROCESSING_BUDGET_SECONDS
        self.assertGreater(countdown, expected - 5)
        self.assertLessEqual(countdown, expected)

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
        ), patch(
            "apps.hosted_automation.signals.HostedAutomationJob.objects.filter"
        ) as filter_jobs, patch.object(
            dispatch_due_hosted_ai, "apply_async"
        ) as apply_async:
            hosted_automation_job_wakeup(
                sender=HostedAutomationJob,
                instance=instance,
                created=False,
                update_fields={"available_at", "updated_at"},
            )

        filter_jobs.assert_not_called()
        apply_async.assert_called_once()
        countdown = apply_async.call_args.kwargs["countdown"]
        self.assertGreater(countdown, (12 * 60 * 60) - 5)
