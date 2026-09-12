from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.welcome_message_service import send_new_lead_welcome
from services.crm.lead_service import create_lead


class NewLeadWelcomeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Welcome Org")
        self.user = User.objects.create_user(
            email="welcome-admin@example.com",
            password="test-password",
            name="Welcome Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Welcome Sales",
            country_code="+91",
            phone_number="9876543210",
            owner=self.user,
        )
        self.new_stage = Stage.objects.get(pipeline=self.pipeline, name="New leads")
        self.qualified_stage = Stage.objects.get(pipeline=self.pipeline, name="Qualified")

    def _lead(self, *, stage=None, phone="+919000000001"):
        return Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=stage or self.new_stage,
            name="Jane Customer",
            phone=phone,
        )

    def _api_account(self, *, number="+919876543210"):
        return WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="API Sender",
            phone_number_id="123456789",
            display_phone_number=number,
            waba_id="987654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _approved_template(self, account):
        template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=account,
            created_by=self.user,
            name="new_lead_welcome",
            category=WhatsAppTemplate.Category.UTILITY,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Hi {{lead_first_name}}, welcome to {{org_name}}",
            meta_template_id="meta-template-welcome",
        )
        WhatsAppTemplateMetadata.objects.create(
            template=template,
            local_status=WhatsAppTemplateMetadata.LocalStatus.SYNCED,
            language="en_US",
            placeholder_mapping={"1": "lead_first_name", "2": "org_name"},
        )
        return template

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_api_new_lead_queues_selected_approved_template(self, delay):
        account = self._api_account()
        template = self._approved_template(account)
        account.welcome_message = template.name
        account.save(update_fields=["welcome_message", "updated_at"])
        lead = self._lead()

        result = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(result["status"], "queued")
        message = WhatsAppMessage.objects.get(id=result["message_id"])
        self.assertEqual(message.account_id, account.id)
        self.assertEqual(message.media_payload["transport"], "template")
        self.assertEqual(message.media_payload["template_name"], template.name)
        self.assertEqual(message.raw_payload["shvya_welcome"]["trigger"], "lead_created")
        delay.assert_called_once_with(str(message.id))

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_welcome_is_idempotent_for_same_lead_and_account(self, delay):
        account = self._api_account()
        template = self._approved_template(account)
        account.welcome_message = template.name
        account.save(update_fields=["welcome_message", "updated_at"])
        lead = self._lead()

        first = send_new_lead_welcome(lead_id=lead.id)
        second = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second, {"status": "skipped", "reason": "welcome_already_queued"})
        self.assertEqual(
            WhatsAppMessage.objects.filter(
                lead=lead,
                raw_payload__shvya_welcome__trigger="lead_created",
            ).count(),
            1,
        )
        self.assertEqual(delay.call_count, 1)

    def test_non_new_leads_stage_never_sends_welcome(self):
        self._api_account()
        lead = self._lead(stage=self.qualified_stage)

        result = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(result, {"status": "skipped", "reason": "not_new_leads_stage"})
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())

    def test_pipeline_number_must_exactly_match_connected_account(self):
        account = self._api_account(number="+919999999999")
        template = self._approved_template(account)
        account.welcome_message = template.name
        account.save(update_fields=["welcome_message", "updated_at"])
        lead = self._lead()

        result = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(
            result,
            {"status": "skipped", "reason": "pipeline_number_not_connected"},
        )
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())

    @patch("apps.channels.hosted_send_tasks.send_hosted_whatsapp_message_task.delay")
    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_hosted_welcome_is_generated_from_organization_information(
        self, provider_class, hosted_delay
    ):
        OrgInfo.objects.update_or_create(
            organization=self.org,
            defaults={
                "about": "We help businesses automate customer engagement.",
                "bot_languages": "English",
                "engagement_instructions": "Keep messages concise and professional.",
            },
        )
        account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sender",
            phone_number_id="+919876543210",
            display_phone_number="+919876543210",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        provider = provider_class.return_value
        provider.generate_text.return_value = AITextResult(
            text="Hi Jane, welcome to Welcome Org. We're glad to connect with you.",
            model="test-model",
        )
        lead = self._lead()

        result = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(result["status"], "queued")
        message = WhatsAppMessage.objects.get(id=result["message_id"])
        self.assertEqual(message.account_id, account.id)
        self.assertEqual(
            message.raw_payload["shvya_welcome"]["source"],
            "organization_information_ai",
        )
        self.assertEqual(message.body, provider.generate_text.return_value.text)
        hosted_delay.assert_called_once_with(str(message.id))
        prompt_input = provider.generate_text.call_args.kwargs["input_text"]
        self.assertIn("We help businesses automate customer engagement.", prompt_input)
        self.assertIn("Keep messages concise and professional.", prompt_input)

    @patch("apps.channels.welcome_tasks.send_lead_welcome_task.delay")
    def test_central_create_lead_schedules_welcome_after_commit(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            lead = create_lead(
                organization=self.org,
                pipeline=self.pipeline,
                stage=self.new_stage,
                name="Scheduled Lead",
                phone="+919000000099",
            )

        delay.assert_called_once_with(str(lead.id))

    @patch("apps.channels.welcome_tasks.send_lead_welcome_task.delay")
    def test_central_create_lead_does_not_schedule_outside_new_leads(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            create_lead(
                organization=self.org,
                pipeline=self.pipeline,
                stage=self.qualified_stage,
                name="Qualified Lead",
                phone="+919000000100",
            )

        delay.assert_not_called()


class WelcomeTemplateSettingsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Welcome UI Org")
        self.user = User.objects.create_user(
            email="welcome-ui@example.com",
            password="test-password",
            name="Welcome UI Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="UI Sales",
            country_code="+91",
            phone_number="9876543210",
            owner=self.user,
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="UI Sender",
            phone_number_id="123456",
            display_phone_number="+919876543210",
            waba_id="654321",
            access_token="test-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=self.org,
            account=self.account,
            created_by=self.user,
            name="approved_ui_welcome",
            status=WhatsAppTemplate.Status.APPROVED,
            attachment_type=WhatsAppTemplate.AttachmentType.NONE,
            body="Welcome",
            meta_template_id="meta-ui-welcome",
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def test_admin_can_select_approved_template_for_linked_number(self):
        response = self.client.post(
            reverse("whatsapp-welcome-template", args=[self.account.id]),
            {"template_name": self.template.name},
        )

        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.welcome_message, self.template.name)

    def test_unlinked_number_cannot_save_welcome_template(self):
        self.account.display_phone_number = "+919999999999"
        self.account.save(update_fields=["display_phone_number", "updated_at"])

        response = self.client.post(
            reverse("whatsapp-welcome-template", args=[self.account.id]),
            {"template_name": self.template.name},
        )

        self.assertEqual(response.status_code, 302)
        self.account.refresh_from_db()
        self.assertEqual(self.account.welcome_message, "")
