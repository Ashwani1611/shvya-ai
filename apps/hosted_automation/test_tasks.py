from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase
from django.utils import timezone

from apps.channels.models import WhatsAppMessage
from apps.hosted_automation.execution import HostedAIContextBuilder
from apps.hosted_automation.models import HostedAutomationJob
from apps.hosted_automation.signals import hosted_automation_job_wakeup


class HostedAIContextBuilderTests(SimpleTestCase):
    def test_messages_are_scoped_to_exact_hosted_account(self):
        builder = HostedAIContextBuilder(account_id="hosted-account-1")
        organization = SimpleNamespace(pk="org-1")
        lead = SimpleNamespace(pk="lead-1")

        queryset = MagicMock()
        ordered = MagicMock()
        ordered.__getitem__.return_value = ["newest", "oldest"]
        queryset.order_by.return_value = ordered

        with patch.object(WhatsAppMessage.objects, "filter", return_value=queryset) as filter_messages:
            messages = builder._get_messages(
                organization=organization,
                lead=lead,
                limit=12,
            )

        filter_messages.assert_called_once_with(
            organization=organization,
            account_id="hosted-account-1",
            lead=lead,
        )
        self.assertEqual(messages, ["oldest", "newest"])

    def test_failsoft_latest_inbound_is_scoped_to_exact_account(self):
        builder = HostedAIContextBuilder(account_id="hosted-account-1")
        organization = SimpleNamespace(pk="org-1")
        lead = SimpleNamespace(pk="lead-1")
        expected = object()

        queryset = MagicMock()
        queryset.select_related.return_value.order_by.return_value.first.return_value = expected

        with patch.object(WhatsAppMessage.objects, "filter", return_value=queryset) as filter_messages:
            actual = builder.latest_inbound_for_fallback(
                organization=organization,
                lead=lead,
            )

        self.assertIs(actual, expected)
        filter_messages.assert_called_once_with(
            organization=organization,
            account_id="hosted-account-1",
            lead=lead,
            direction=WhatsAppMessage.Direction.INBOUND,
        )


class HostedAIDebounceTests(SimpleTestCase):
    def test_job_wakeup_respects_available_at_without_15_second_acceleration(self):
        available_at = timezone.now() + timezone.timedelta(seconds=5)
        instance = SimpleNamespace(
            status=HostedAutomationJob.Status.QUEUED,
            available_at=available_at,
        )

        with (
            patch("apps.hosted_automation.signals.transaction.on_commit") as on_commit,
            patch("apps.hosted_automation.signals.HostedAutomationJob.objects.filter") as jobs,
        ):
            hosted_automation_job_wakeup(
                sender=HostedAutomationJob,
                instance=instance,
                created=True,
                update_fields=None,
            )

        jobs.assert_not_called()
        self.assertEqual(instance.available_at, available_at)
        on_commit.assert_called_once()

        callback = on_commit.call_args.args[0]
        with patch("apps.hosted_automation.signals.dispatch_due_hosted_ai.apply_async") as dispatch:
            callback()
        delay = dispatch.call_args.kwargs["countdown"]
        self.assertGreater(delay, 0)
        self.assertLessEqual(delay, 5)
