"""Regressions for Insights failed-template bulk and navigation actions."""
from urllib.parse import urlencode

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline
from apps.organizations.models import Organization


class FailedTemplateActionTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Insights Failed Actions")
        self.user = User.objects.create_user(
            email="failed-actions@example.com",
            password="test-password",
            name="Insights Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.original_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Original Sender",
            phone_number_id="111222333",
            display_phone_number="+919100000001",
            waba_id="waba-original",
            access_token="token-original",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.current_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Current Pipeline Sender",
            phone_number_id="444555666",
            display_phone_number="+919200000002",
            waba_id="waba-current",
            access_token="token-current",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            name="Current Pipeline",
            country_code="+91",
            phone_number=self.current_account.display_phone_number,
            owner=self.user,
        )
        self.stage = self.pipeline.stages.order_by("display_order", "pk").first()
        self.assertIsNotNone(self.stage)
        self.lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Selected Lead",
            phone="+919300000003",
            email="selected@example.com",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def failed_message(self, *, lead=None, name="campaign_template"):
        lead = lead or self.lead
        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.original_account,
            lead=lead,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number=self.original_account.display_phone_number,
            to_number=lead.phone,
            body="Template send",
            status=WhatsAppMessage.Status.FAILED,
            error="Meta rejected the template.",
            media_payload={
                "transport": "template",
                "template_name": name,
            },
        )

    def test_bulk_controls_start_hidden_and_actions_use_current_pipeline_routes(self):
        self.failed_message()

        response = self.client.get(reverse("crm-analytics"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            'id="failed-bulk-actions" class="ins-failed-toolbar" style="margin-bottom: 12px;" hidden',
        )

        message = response.context["failed_page"].object_list[0]
        expected_chat = (
            reverse("whatsapp-chat-detail", args=[self.lead.pk])
            + "?"
            + urlencode({"account": self.current_account.pk})
        )
        crm_query = {
            "pipeline": str(self.pipeline.pk),
            "lead": str(self.lead.pk),
            "stage": str(self.stage.pk),
        }
        expected_crm = reverse("crm-dashboard") + "?" + urlencode(crm_query)

        self.assertEqual(message.insights_chat_url, expected_chat)
        self.assertEqual(message.insights_crm_url, expected_crm)
        self.assertNotIn("wa.me", message.insights_chat_url)

    def test_export_contains_only_selected_failed_messages(self):
        selected = self.failed_message(name="selected_template")
        other_lead = Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Other Lead",
            phone="+919300000004",
            email="other@example.com",
        )
        self.failed_message(lead=other_lead, name="other_template")

        response = self.client.get(
            reverse("crm-analytics-failed-whatsapp-export"),
            {"message_ids": [str(selected.pk)]},
        )
        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Selected Lead", csv_text)
        self.assertIn("selected_template", csv_text)
        self.assertNotIn("Other Lead", csv_text)
        self.assertNotIn("other_template", csv_text)

    def test_invalid_selected_export_ids_do_not_expand_or_error(self):
        selected = self.failed_message()
        response = self.client.get(
            reverse("crm-analytics-failed-whatsapp-export"),
            {"message_ids": ["not-a-uuid", str(selected.pk)]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Selected Lead", response.content.decode("utf-8"))
