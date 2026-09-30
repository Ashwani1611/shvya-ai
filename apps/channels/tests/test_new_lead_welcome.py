from tests.playbook_fixtures import build_ai_playbook

from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.models import OrgInfo
from apps.ai_engagement.services.ai_provider import AITextResult
from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from apps.hosted_automation.models import HostedAutomationJob
from services.channels.welcome_message_service import execute_queued_welcome, send_new_lead_welcome
from services.crm.lead_service import create_lead, upsert_lead


class NewLeadWelcomeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(package="dfy", name="Welcome Org")
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

    def _hosted_account(self):
        return WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sender",
            phone_number_id="+919876543210",
            display_phone_number="+919876543210",
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

    def _processing_job(self, result):
        job = HostedAutomationJob.objects.get(pk=result["job_id"])
        job.status = HostedAutomationJob.Status.PROCESSING
        job.available_at = timezone.now()
        job.started_at = timezone.now()
        job.save(update_fields=["status", "available_at", "started_at", "updated_at"])
        return job

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_api_welcome_materializes_selected_template_only_when_due(self, delay):
        account = self._api_account()
        template = self._approved_template(account)
        account.welcome_message = template.name
        account.save(update_fields=["welcome_message", "updated_at"])
        lead = self._lead()

        result = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(result["status"], "queued")
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())
        delay.assert_not_called()
        job = self._processing_job(result)
        executed = execute_queued_welcome(job=job)
        message = WhatsAppMessage.objects.get(id=executed["message_id"])
        self.assertEqual(message.account_id, account.id)
        self.assertEqual(message.media_payload["transport"], "template")
        self.assertEqual(message.media_payload["template_name"], template.name)
        self.assertEqual(message.raw_payload["shvya_welcome"]["trigger"], "lead_created")
        self.assertEqual(message.raw_payload["shvya_welcome"]["job_id"], str(job.id))
        # The durable worker owns delivery and persists the message ID first.
        delay.assert_not_called()

    def test_welcome_intent_is_idempotent_for_same_lead_and_account(self):
        account = self._api_account()
        template = self._approved_template(account)
        account.welcome_message = template.name
        account.save(update_fields=["welcome_message", "updated_at"])
        lead = self._lead()

        first = send_new_lead_welcome(lead_id=lead.id)
        second = send_new_lead_welcome(lead_id=lead.id)

        self.assertEqual(first["status"], "queued")
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertEqual(HostedAutomationJob.objects.filter(lead=lead).count(), 1)
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())
        job = self._processing_job(first)
        materialized = execute_queued_welcome(job=job)
        replayed = execute_queued_welcome(job=job)
        self.assertEqual(replayed["message_id"], materialized["message_id"])
        self.assertEqual(WhatsAppMessage.objects.filter(lead=lead).count(), 1)

    def test_non_new_leads_stage_never_sends_welcome(self):
        self._api_account()
        lead = self._lead(stage=self.qualified_stage)
        result = send_new_lead_welcome(lead_id=lead.id)
        self.assertEqual(result, {"status": "skipped", "reason": "not_new_leads_stage"})
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())

    def test_pipeline_number_must_exactly_match_connected_account(self):
        self._api_account(number="+919999999999")
        lead = self._lead()
        result = send_new_lead_welcome(lead_id=lead.id)
        self.assertEqual(result, {"status": "skipped", "reason": "pipeline_number_not_connected"})
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_hosted_welcome_is_generated_only_when_due_from_organization_information(self, provider_class):
        OrgInfo.objects.update_or_create(
            organization=self.org,
            defaults={
                "about": "We help businesses automate customer engagement.",
                "bot_languages": "English",
                "ai_playbook": build_ai_playbook(rules="Keep messages concise and professional."),
            },
        )
        account = self._hosted_account()
        provider = provider_class.return_value
        provider.generate_text.return_value = AITextResult(
            text="Hi Jane, welcome to Welcome Org. We're glad to connect with you.",
            model="test-model",
        )
        lead = self._lead()
        result = send_new_lead_welcome(lead_id=lead.id)
        self.assertEqual(result["status"], "queued")
        provider_class.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.filter(lead=lead).exists())

        executed = execute_queued_welcome(job=self._processing_job(result))
        message = WhatsAppMessage.objects.get(id=executed["message_id"])
        self.assertEqual(message.account_id, account.id)
        self.assertEqual(message.raw_payload["shvya_welcome"]["source"], "organization_information_ai")
        self.assertEqual(message.body, provider.generate_text.return_value.text)
        prompt_input = provider.generate_text.call_args.kwargs["input_text"]
        self.assertIn("We help businesses automate customer engagement.", prompt_input)
        self.assertIn("Keep messages concise and professional.", prompt_input)

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_bulk_leads_create_only_queue_intents(self, provider_class):
        self._hosted_account()
        for index in range(10):
            lead = self._lead(phone=f"+9190000001{index:02d}")
            self.assertEqual(send_new_lead_welcome(lead_id=lead.id)["status"], "queued")
        self.assertEqual(HostedAutomationJob.objects.filter(kind="welcome").count(), 10)
        self.assertFalse(WhatsAppMessage.objects.exists())
        provider_class.assert_not_called()

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_future_welcome_cannot_materialize_even_if_called_early(self, provider_class):
        self._hosted_account()
        result = send_new_lead_welcome(lead_id=self._lead().id)
        job = self._processing_job(result)
        job.available_at = timezone.now() + timedelta(seconds=45)
        job.save(update_fields=["available_at", "updated_at"])
        self.assertEqual(execute_queued_welcome(job=job)["reason"], "welcome_not_due")
        provider_class.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.exists())

    def _disable_control(self, control, lead):
        if control == "organization":
            OrgInfo.objects.update_or_create(organization=self.org, defaults={"ai_enabled": False})
        elif control == "pipeline":
            Pipeline.objects.filter(pk=self.pipeline.pk).update(ai_enabled=False)
        elif control == "stage":
            Stage.objects.filter(pk=self.new_stage.pk).update(ai_on=False)
        else:
            Lead.objects.filter(pk=lead.pk).update(ai_enabled=False)

    def _enable_controls(self):
        OrgInfo.objects.update_or_create(organization=self.org, defaults={"ai_enabled": True})
        Pipeline.objects.filter(pk=self.pipeline.pk).update(ai_enabled=True)
        Stage.objects.filter(pk=self.new_stage.pk).update(ai_on=True)

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_each_ai_toggle_blocks_welcome_enqueue(self, provider_class):
        self._hosted_account()
        for index, control in enumerate(("organization", "pipeline", "stage", "lead")):
            with self.subTest(control=control):
                self._enable_controls()
                lead = self._lead(phone=f"+9190000002{index:02d}")
                self._disable_control(control, lead)
                result = send_new_lead_welcome(lead_id=lead.id)
                self.assertEqual(result["reason"], f"{control}_ai_disabled")
                self.assertFalse(HostedAutomationJob.objects.filter(lead=lead).exists())
        provider_class.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.exists())

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_each_ai_toggle_is_rechecked_when_welcome_becomes_due(self, provider_class):
        self._hosted_account()
        for index, control in enumerate(("organization", "pipeline", "stage", "lead")):
            with self.subTest(control=control):
                self._enable_controls()
                lead = self._lead(phone=f"+9190000003{index:02d}")
                job = self._processing_job(send_new_lead_welcome(lead_id=lead.id))
                self._disable_control(control, lead)
                result = execute_queued_welcome(job=job)
                self.assertEqual(result["reason"], f"{control}_ai_disabled")
        provider_class.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.exists())

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_disabling_stage_during_generation_prevents_chat_row(self, provider_class):
        self._hosted_account()
        lead = self._lead()
        job = self._processing_job(send_new_lead_welcome(lead_id=lead.id))

        def generate(**kwargs):
            self._disable_control("stage", lead)
            return AITextResult(text="Welcome", model="test-model")

        provider_class.return_value.generate_text.side_effect = generate
        self.assertEqual(execute_queued_welcome(job=job)["reason"], "stage_ai_disabled")
        self.assertFalse(WhatsAppMessage.objects.exists())

    @patch("services.channels.welcome_message_service.OpenAIProvider")
    def test_sender_is_rechecked_after_queued_lead_moves_pipeline(self, provider_class):
        self._hosted_account()
        lead = self._lead()
        job = self._processing_job(send_new_lead_welcome(lead_id=lead.id))
        Pipeline.objects.filter(pk=self.pipeline.pk).update(phone_number="7777777777")
        self.assertEqual(execute_queued_welcome(job=job)["reason"], "pipeline_number_not_connected")
        provider_class.assert_not_called()
        self.assertFalse(WhatsAppMessage.objects.exists())

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
    def test_external_upsert_new_lead_still_schedules_template_welcome(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            lead, created = upsert_lead(
                organization=self.org,
                pipeline=self.pipeline,
                stage=self.new_stage,
                name="External Lead",
                phone="+919000000101",
                lead_source="system",
            )

        self.assertTrue(created)
        delay.assert_called_once_with(str(lead.id))

    @patch("apps.channels.welcome_tasks.send_lead_welcome_task.delay")
    def test_whatsapp_api_inbound_new_lead_skips_template_welcome(self, delay):
        with self.captureOnCommitCallbacks(execute=True):
            lead, created = upsert_lead(
                organization=self.org,
                pipeline=self.pipeline,
                stage=self.new_stage,
                name="919000000102",
                phone="+919000000102",
                lead_source="whatsapp_api",
            )

        self.assertTrue(created)
        self.assertEqual(lead.lead_source, "whatsapp_api")
        delay.assert_not_called()

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
        self.org = Organization.objects.create(package="dfy", name="Welcome UI Org")
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
