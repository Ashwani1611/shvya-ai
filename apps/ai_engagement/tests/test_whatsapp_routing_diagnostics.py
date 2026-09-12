import json
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from apps.ai_engagement.models import AICreditWallet
from apps.ai_engagement.services.ai_permissions import AIPermissionService
from apps.ai_engagement.services.diagnostics import diagnose_engagement
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization
from services.channels.whatsapp_service import handle_inbound_message, resolve_pipeline


@override_settings(OPENAI_API_KEY="test-key-never-sent", CELERY_TASK_ALWAYS_EAGER=False)
class WhatsAppRoutingDiagnosticsTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Routing")
        self.fallback, _ = Pipeline.objects.get_or_create(
            organization=self.org, name="Leads"
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Sales",
            country_code="+91",
            phone_number="9876543210",
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline, name="New", display_order=0, ai_on=True
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="api",
            phone_number_id="123456789012345",
            display_phone_number="+91 98765 43210",
            status="connected",
            is_active=True,
        )

    def _lead(self):
        return Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Customer",
            phone="+918700274739",
            ai_enabled=True,
        )

    def _inbound(self, lead):
        return WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.account,
            lead=lead,
            direction="inbound",
            external_id="diagnostic-inbound",
            body="Hello",
            status="received",
            from_number=lead.phone,
            to_number=self.account.display_phone_number,
        )

    def test_formatted_number_routes_to_matching_pipeline_before_fallback(self):
        self.assertEqual(
            resolve_pipeline(organization=self.org, to_number="+91 98765 43210"),
            self.pipeline,
        )

    @patch("apps.ai_engagement.background_signals.queue_background_enrichment")
    @patch("apps.ai_engagement.tasks.generate_ai_engagement_response.apply_async")
    def test_meta_resource_id_cannot_misroute_new_lead(self, engage, enrichment):
        with self.captureOnCommitCallbacks(execute=True):
            message = handle_inbound_message(
                organization=self.org,
                account=self.account,
                external_id="routing-inbound",
                from_number="918700274739",
                to_number=self.account.phone_number_id,
                body="Tell me more",
                raw_payload={},
            )
        self.assertIsNotNone(message.lead)
        self.assertEqual(message.lead.pipeline_id, self.pipeline.id)
        self.assertTrue(
            AIPermissionService()
            .evaluate(organization=self.org, lead=message.lead)
            .allowed
        )
        enrichment.assert_called_once_with(lead_id=str(message.lead_id))
        engage.assert_called_once_with(
            args=[str(message.lead_id)],
            countdown=0,
        )

    def test_number_match_is_organization_scoped(self):
        other = Organization.objects.create(name="Other")
        self.assertEqual(
            resolve_pipeline(
                organization=other, to_number="919876543210"
            ).organization_id,
            other.id,
        )

    def test_missing_wallet_is_reported_without_creating_one(self):
        lead = self._lead()
        self._inbound(lead)
        AICreditWallet.objects.filter(organization=self.org).delete()
        report = diagnose_engagement(lead=lead)
        self.assertIn("organization_ai_credits_empty", report["blockers"])
        self.assertFalse(AICreditWallet.objects.filter(organization=self.org).exists())
        self.assertNotIn("test-key-never-sent", json.dumps(report))

    def test_hosted_diagnostics_ignore_stage_toggle_but_show_missing_job(self):
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        lead = self._lead()
        self._inbound(lead)
        self.stage.ai_on = False
        self.stage.save(update_fields=["ai_on"])
        lead.refresh_from_db()
        report = diagnose_engagement(lead=lead)
        self.assertNotIn("stage_ai_disabled", report["blockers"])
        self.assertIn(
            "no_hosted_ai_job_check_live_inbound_and_lead_mapping", report["blockers"]
        )

    def test_command_scopes_lead_to_organization(self):
        lead = self._lead()
        other = Organization.objects.create(name="Other")
        with self.assertRaises(CommandError):
            call_command(
                "diagnose_whatsapp_ai",
                organization_id=str(other.id),
                lead_id=str(lead.id),
            )
        out = StringIO()
        call_command(
            "diagnose_whatsapp_ai",
            organization_id=str(self.org.id),
            lead_id=str(lead.id),
            stdout=out,
        )
        self.assertEqual(json.loads(out.getvalue())["lead_id"], str(lead.id))

    @patch("apps.channels.views_flat._verify_signature", return_value=True)
    def test_real_meta_payload_stores_business_number(self, signature):
        from django.test import RequestFactory
        from apps.channels.views_flat import _handle_webhook_delivery

        payload = {
            "entry": [
                {
                    # The phone_number_id is sufficient for message routing;
                    # an old/stale WABA value must not suppress the chat.
                    "id": "stale-waba-id",
                    "changes": [
                        {
                            "value": {
                                "metadata": {
                                    "phone_number_id": self.account.phone_number_id,
                                    "display_phone_number": "919876543210",
                                },
                                "messages": [
                                    {
                                        "id": "meta-webhook-test",
                                        "from": "918700274739",
                                        "type": "text",
                                        "text": {"body": "Hello"},
                                    }
                                ],
                            }
                        }
                    ]
                }
            ]
        }
        request = RequestFactory().post(
            "/webhook/", data=json.dumps(payload), content_type="application/json"
        )
        response = _handle_webhook_delivery(request)
        self.assertEqual(response.status_code, 200)
        message = WhatsAppMessage.objects.get(external_id="meta-webhook-test")
        self.assertEqual(message.to_number, "919876543210")
        self.assertEqual(message.lead.pipeline_id, self.pipeline.id)

    @patch("apps.channels.tasks.sync_whatsapp_templates_task.delay")
    @patch("apps.channels.views_flat._verify_signature", return_value=True)
    def test_template_status_webhook_queues_a_meta_sync(self, signature, delay):
        from django.test import RequestFactory
        from apps.channels.views_flat import _handle_webhook_delivery

        self.account.waba_id = "waba-routing"
        self.account.save(update_fields=["waba_id"])
        payload = {
            "entry": [
                {
                    "id": self.account.waba_id,
                    "changes": [
                        {
                            "field": "message_template_status_update",
                            "value": {"event": "APPROVED"},
                        }
                    ],
                }
            ]
        }
        request = RequestFactory().post(
            "/webhook/", data=json.dumps(payload), content_type="application/json"
        )
        with self.captureOnCommitCallbacks(execute=True):
            response = _handle_webhook_delivery(request)

        self.assertEqual(response.status_code, 200)
        delay.assert_called_once_with(str(self.account.id))
