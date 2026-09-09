from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from apps.ai_engagement import tasks as ai_tasks
from apps.ai_engagement.services.context import AIContextBuilder
from apps.channels.models import WhatsAppAccount
from apps.hosted_automation.tasks import _hosted_ai_execution_scope
from services.channels import whatsapp_service


class HostedAIExecutionScopeTests(SimpleTestCase):
    def _job(self):
        organization = SimpleNamespace(pk="org-1")
        lead = SimpleNamespace(pk="lead-1")
        return SimpleNamespace(
            organization=organization,
            organization_id="org-1",
            account_id="hosted-account-1",
            lead_id="lead-1",
        ), organization, lead

    def test_scope_pins_latest_message_to_hosted_account(self):
        job, _organization, lead = self._job()
        lead.whatsapp_messages = MagicMock()
        expected_message = object()
        lead.whatsapp_messages.filter.return_value.order_by.return_value.first.return_value = (
            expected_message
        )

        with _hosted_ai_execution_scope(job):
            actual = ai_tasks._latest_whatsapp_message(lead=lead)

        self.assertIs(actual, expected_message)
        lead.whatsapp_messages.filter.assert_called_once_with(
            organization=job.organization,
            account_id=job.account_id,
        )

    def test_scope_pins_account_resolution_to_hosted_job_account(self):
        job, organization, lead = self._job()
        expected_account = object()
        queryset = MagicMock()
        queryset.first.return_value = expected_account

        with patch.object(
            WhatsAppAccount.objects,
            "filter",
            return_value=queryset,
        ) as filter_accounts:
            with _hosted_ai_execution_scope(job):
                actual = whatsapp_service.resolve_account_for_lead(
                    organization=organization,
                    lead=lead,
                )

        self.assertIs(actual, expected_account)
        filter_accounts.assert_called_once_with(
            pk=job.account_id,
            organization=organization,
            connection_type="hosted",
            is_active=True,
            status=WhatsAppAccount.Status.CONNECTED,
        )

    def test_scope_rejects_wrong_lead_or_organization(self):
        job, organization, lead = self._job()

        with _hosted_ai_execution_scope(job):
            wrong_lead = whatsapp_service.resolve_account_for_lead(
                organization=organization,
                lead=SimpleNamespace(pk="lead-2"),
            )
            wrong_org = whatsapp_service.resolve_account_for_lead(
                organization=SimpleNamespace(pk="org-2"),
                lead=lead,
            )

        self.assertIsNone(wrong_lead)
        self.assertIsNone(wrong_org)

    def test_scope_restores_shared_helpers_after_failure(self):
        job, _organization, _lead = self._job()
        original_latest = ai_tasks._latest_whatsapp_message
        original_existing = ai_tasks._has_existing_ai_response
        original_resolver = whatsapp_service.resolve_account_for_lead
        original_get_messages = AIContextBuilder._get_messages

        with self.assertRaises(RuntimeError):
            with _hosted_ai_execution_scope(job):
                raise RuntimeError("boom")

        self.assertIs(ai_tasks._latest_whatsapp_message, original_latest)
        self.assertIs(ai_tasks._has_existing_ai_response, original_existing)
        self.assertIs(whatsapp_service.resolve_account_for_lead, original_resolver)
        self.assertIs(AIContextBuilder._get_messages, original_get_messages)
