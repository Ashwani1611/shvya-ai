"""Database/API regressions for real campaign ledgers. Provider I/O is mocked."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import BytesIO
from threading import Event
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, transaction
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from openpyxl import Workbook

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.campaign_models import (
    CampaignAttempt, CampaignEvent, CampaignPlan,
    CampaignSenderGate, CampaignSuppression, CampaignUpload,
)
from apps.channels.models import BulkMessageCampaign, BulkMessageRecipient, WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.channels.template_models import WhatsAppTemplateMetadata
from apps.crm.models import AttributeDefinition, Lead, Pipeline, PipelinePermission
from apps.organizations.models import Organization
from services.channels import whatsapp_service
from services.channels.campaign_audience import create_upload, owned_upload, review_upload, source_catalog, visible_campaigns
from services.channels.campaign_delivery import recover_expired_claims, send_delivery
from services.channels.campaign_events import install_campaign_webhook, reconcile_events, record_status_event
from services.channels.campaign_policy import CampaignInputError
from services.channels.campaign_reporting import campaign_report, delivery_counts
from services.channels.campaign_service import (
    cancel_campaign, confirm_campaign, default_bindings, get_template, prepare_campaign_chunk,
    preview_campaign, retry_recipients, template_snapshot,
)


class CampaignFixture:
    def setUp(self):
        super().setUp()
        self.org = Organization.objects.create(name="Campaign Test Organization")
        self.user = User.objects.create_user(email="campaign-test@example.com", password="test-password",
                                             name="Campaign Admin", organization=self.org, role=User.Role.ADMIN)
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Campaign Sales",
            phone_number="123456789",
        )
        self.stage = self.pipeline.stages.order_by("display_order", "pk").first()
        self.assertIsNotNone(self.stage)
        self.attribute, _ = AttributeDefinition.objects.get_or_create(organization=self.org, key="campaign_company",
                                                                       defaults={"name": "Campaign Company", "field_type": "text"})
        self.account = WhatsAppAccount.objects.create(organization=self.org, business_name="Campaign Test Number",
            phone_number_id="123456789", waba_id="987654321", access_token="test-token", status="connected", is_active=True)
        self.template = WhatsAppTemplate.objects.create(organization=self.org, account=self.account, name="campaign_test_notice",
            category="utility", status="approved", meta_template_id="template-test-id", body="Hello {{1}}")
        self.metadata = WhatsAppTemplateMetadata.objects.create(template=self.template, local_status="synced", language="en_US",
            components=[{"type": "BODY", "text": "Hello {{1}}"}], placeholder_mapping={"1": "lead_name"})
        self.session = SessionStore()
        set_authenticated_user(self.session, self.user)
        self.session.save()
        self.client.cookies["shvya_crm_sessionid"] = self.session.session_key
        for target in ["services.channels.campaign_service._wake", "services.channels.realtime.queue_message_publish"]:
            mocked = patch(target)
            mocked.start()
            self.addCleanup(mocked.stop)

    def url(self, name, **kwargs):
        return reverse(f"whatsapp-campaign-{name}", kwargs=kwargs or None)

    def post(self, name, data, **kwargs):
        return self.client.post(self.url(name, **kwargs), json.dumps(data), content_type="application/json")

    def audience(self, text="Name,Phone,Email,Company\nAsha,+919000000001,asha@example.com,Example Company\n", **changes):
        upload = create_upload(user=self.user, uploaded_file=SimpleUploadedFile("leads.csv", text.encode(), content_type="text/csv"))
        config = {"mapping": {"name": "Name", "phone": "Phone", "email": "Email", "attr:campaign_company": "Company"},
                  "pipeline_id": str(self.pipeline.pk), "stage_id": str(self.stage.pk), "mode": "both"}
        config.update(changes)
        return review_upload(user=self.user, token=upload.pk, data=config)

    def confirmation(self, upload, **changes):
        bindings = default_bindings(template_snapshot(self.template), source_catalog(self.user))
        preview = preview_campaign(user=self.user, upload=upload, template_id=self.template.pk, bindings=bindings)
        data = {"upload_id": str(upload.pk), "template_id": str(self.template.pk), "bindings": bindings,
                "name": "Test campaign", "audience_digest": upload.review_digest, "preview_digest": preview["digest"],
                "confirmed": True, "consent_confirmed": True, "accept_exclusions": True,
                "timezone": "Asia/Kolkata", "auto_retry": False, "retry_attempts": 3, "retry_delay_hours": 24}
        data.update(changes)
        return data

    def campaign(self, upload=None, **changes):
        upload = upload or self.audience()
        campaign, _ = confirm_campaign(user=self.user, data=self.confirmation(upload, **changes))
        prepare_campaign_chunk(campaign.pk)
        campaign.refresh_from_db()
        return campaign

    def outbound(self, lead, **changes):
        data = {"organization": self.org, "account": self.account, "lead": lead, "direction": "outbound",
                "from_number": self.account.phone_number_id, "to_number": lead.phone, "body": "Test", "status": "sent"}
        data.update(changes)
        return WhatsAppMessage.objects.create(**data)

    def evidence(self, campaign, *, code=None):
        delivery = campaign.campaign_delivery_rows.get()
        message = self.outbound(delivery.lead, external_id="wamid.campaign.evidence")
        attempt = CampaignAttempt.objects.create(delivery=delivery, number=1, message=message, provider_id=message.external_id,
                                                 accepted_at=timezone.now())
        delivery.state, delivery.attempt_count, delivery.accepted_at = "accepted", 1, timezone.now()
        if code:
            delivery.state, delivery.failed_at, delivery.error_code, delivery.http_status = "failed", timezone.now(), code, 400
        delivery.save()
        return delivery, message, attempt


class BulkCampaignTests(CampaignFixture, TestCase):
    def test_review_counts_duplicates_invalid_and_modes_without_crm_writes(self):
        text = "Name,Phone,Email,Company\nAsha,+919000000001,,Example\nDuplicate,919000000001,,\nInvalid,no-number,,\n"
        upload = self.audience(text, mode="new_only")
        self.assertEqual(upload.review_stats["rows"], 3)
        self.assertEqual(upload.review_stats["eligible"], 1)
        self.assertEqual(upload.review_stats["duplicate"], 1)
        self.assertEqual(upload.review_stats["invalid"], 1)
        self.assertEqual(Lead.objects.filter(organization=self.org).count(), 0)
        self.assertEqual(WhatsAppMessage.objects.count(), 0)

    def test_creation_preparation_and_confirmation_are_idempotent(self):
        upload = self.audience()
        data = self.confirmation(upload)
        with patch("services.crm.lead_service._schedule_new_lead_welcome") as welcome:
            first, created = confirm_campaign(user=self.user, data=data)
            second, duplicate_created = confirm_campaign(user=self.user, data=data)
            self.assertEqual(first.pk, second.pk)
            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertFalse(prepare_campaign_chunk(first.pk))
            self.assertFalse(prepare_campaign_chunk(first.pk))
            welcome.assert_not_called()
        self.assertEqual(first.campaign_delivery_rows.count(), 1)
        self.assertEqual(first.recipients.count(), 1)
        lead = first.recipients.get().lead
        self.assertEqual(lead.attributes["campaign_company"], "Example Company")
        self.assertEqual(lead.lead_source, "csv_import")
        upload.refresh_from_db()
        self.assertEqual(upload.rows, [])
        self.assertEqual(upload.reviewed_rows, [])

    def test_existing_lead_requires_explicit_update_and_move(self):
        lead = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage,
                                   name="Original", phone="+919000000001", attributes={"campaign_company": "Original Co"})
        first = self.campaign(self.audience(mode="existing_only"))
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Original")
        self.assertEqual(first.campaign_delivery_rows.get().body, "Hello Original")
        second = self.campaign(self.audience(mode="existing_only", update_existing=True))
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Asha")
        self.assertEqual(lead.attributes["campaign_company"], "Example Company")
        self.assertEqual(second.campaign_plan.stats["existing_leads_updated"], 1)

    def test_new_only_excludes_existing_and_existing_only_excludes_missing(self):
        self.campaign()
        self.assertEqual(self.audience(mode="new_only").review_stats["eligible"], 0)
        upload = self.audience("Name,Phone,Email,Company\nMissing,+919000000002,,\n", mode="existing_only")
        self.assertEqual(upload.review_stats["excluded"], 1)

    def test_edits_after_review_are_not_overwritten(self):
        lead = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage, name="Original", phone="+919000000001")
        upload = self.audience(mode="existing_only", update_existing=True)
        lead.name = "Human edit"
        lead.save(update_fields=["name", "updated_at"])
        campaign = self.campaign(upload)
        lead.refresh_from_db()
        self.assertEqual(lead.name, "Human edit")
        self.assertEqual(campaign.campaign_delivery_rows.get().state, "skipped")

    def test_pipeline_and_stage_columns_apply_to_new_leads(self):
        other = Pipeline.objects.create(organization=self.org, name="Campaign Other")
        stage = other.stages.order_by("display_order", "pk").first()
        text = f"Name,Phone,Email,Company,Pipeline,Stage\nAsha,+919000000001,,Example,{other.name},{stage.name}\n"
        upload = self.audience(text, mapping={"name": "Name", "phone": "Phone", "pipeline": "Pipeline", "stage": "Stage"})
        campaign = self.campaign(upload)
        lead = campaign.recipients.get().lead
        self.assertEqual(lead.pipeline_id, other.pk)
        self.assertEqual(lead.stage_id, stage.pk)

    def test_confirmation_requires_consent_current_review_and_preview(self):
        upload = self.audience()
        for changes in [{"consent_confirmed": False}, {"confirmed": False}, {"audience_digest": "old"}, {"preview_digest": "old"}, {"name": ""}]:
            with self.subTest(changes=changes), self.assertRaises(CampaignInputError):
                confirm_campaign(user=self.user, data=self.confirmation(upload, **changes))
        self.assertFalse(BulkMessageCampaign.objects.exists())

    def test_future_schedule_and_retry_toggle_are_durable(self):
        future = (timezone.now() + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
        campaign = self.campaign(schedule_enabled=True, scheduled_local=future, timezone="UTC", auto_retry=True, retry_attempts=2, retry_delay_hours=48)
        plan = campaign.campaign_plan
        self.assertTrue(plan.auto_retry)
        self.assertEqual(plan.retry_attempts, 2)
        self.assertEqual(plan.retry_delay_hours, 48)
        self.assertGreater(plan.scheduled_for, timezone.now())
        self.assertEqual(send_delivery(campaign.campaign_delivery_rows.get().pk)["status"], "not_due")

    def test_upload_expiry_and_cross_tenant_template_are_rejected(self):
        upload = self.audience()
        CampaignUpload.objects.filter(pk=upload.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(CampaignInputError):
            owned_upload(user=self.user, token=upload.pk)
        other = Organization.objects.create(name="Other organization")
        stranger = User.objects.create_user(email="stranger@example.com", password="test-password", name="Stranger", organization=other, role=User.Role.ADMIN)
        with self.assertRaises(PermissionDenied):
            get_template(user=stranger, template_id=self.template.pk)
        with self.assertRaises(PermissionDenied):
            owned_upload(user=stranger, token=upload.pk)

    def test_disabled_user_hosted_account_and_internal_attributes_are_not_available(self):
        AttributeDefinition.objects.create(organization=self.org, name="Secret", key="api_token", field_type="text")
        self.assertNotIn("attr:api_token", {item["key"] for item in source_catalog(self.user)})
        self.account.connection_type = "hosted"
        self.account.save(update_fields=["connection_type"])
        with self.assertRaises(PermissionDenied):
            get_template(user=self.user, template_id=self.template.pk)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.assertEqual(self.client.get(self.url("options")).status_code, 302)

    def test_opt_out_is_excluded_before_confirm_and_before_dispatch(self):
        CampaignSuppression.objects.create(organization=self.org, phone="+919000000001", reason="STOP")
        self.assertEqual(self.audience().review_stats["suppressed"], 1)
        CampaignSuppression.objects.all().delete()
        campaign = self.campaign()
        CampaignSuppression.objects.create(organization=self.org, phone="+919000000001", reason="STOP")
        with patch.object(whatsapp_service, "send_outbound_message") as sender:
            result = send_delivery(campaign.campaign_delivery_rows.get().pk)
        self.assertEqual(result["status"], "opted_out")
        sender.assert_not_called()

    def test_webhook_evidence_is_deduplicated_and_monotonic(self):
        campaign = self.campaign()
        delivery, message, _ = self.evidence(campaign)
        timestamp = str(int(timezone.now().timestamp()))
        for status in ["read", "delivered", "sent", "read"]:
            record_status_event(account=self.account, external_id=message.external_id, status=status,
                                raw_payload={"timestamp": timestamp, "recipient_id": delivery.phone})
            reconcile_events()
        self.assertEqual(CampaignEvent.objects.filter(kind="status").count(), 3)
        counts = delivery_counts(campaign.campaign_delivery_rows)
        self.assertEqual((counts["total"], counts["sent"], counts["delivered"], counts["read"]), (1, 1, 1, 1))
        message.refresh_from_db()
        self.assertEqual(message.status, "read")

    def test_early_callback_waits_for_its_provider_id(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        record_status_event(account=self.account, external_id="wamid.early", status="delivered", raw_payload={"recipient_id": delivery.phone})
        self.assertEqual(reconcile_events()[0], 0)
        message = self.outbound(delivery.lead, external_id="wamid.early")
        CampaignAttempt.objects.create(delivery=delivery, number=1, message=message)
        delivery.attempt_count = 1
        delivery.save(update_fields=["attempt_count"])
        self.assertEqual(reconcile_events()[0], 1)
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.delivered_at)

    def test_wrong_account_or_recipient_cannot_set_delivery(self):
        campaign = self.campaign()
        delivery, message, _ = self.evidence(campaign)
        other = WhatsAppAccount.objects.create(organization=self.org, phone_number_id="different", connection_type="api", status="connected")
        record_status_event(account=other, external_id=message.external_id, status="read", raw_payload={})
        self.assertFalse(CampaignEvent.objects.exists())
        record_status_event(account=self.account, external_id=message.external_id, status="read", raw_payload={"recipient_id": "+919000000999"})
        reconcile_events()
        delivery.refresh_from_db()
        self.assertIsNone(delivery.read_at)

    def test_reply_attribution_and_synchronous_stop_suppression(self):
        campaign = self.campaign()
        delivery, message, _ = self.evidence(campaign)
        inbound = WhatsAppMessage.objects.create(organization=self.org, account=self.account, lead=delivery.lead,
            direction="inbound", from_number=delivery.phone, to_number=self.account.phone_number_id,
            external_id="wamid.reply", body="STOP", status="received", raw_payload={"context": {"id": message.external_id}})
        self.assertTrue(CampaignSuppression.objects.filter(phone=delivery.phone, organization=self.org, source_message=inbound).exists())
        reconcile_events()
        delivery.refresh_from_db()
        self.assertIsNotNone(delivery.replied_at)
        self.assertEqual(delivery_counts(campaign.campaign_delivery_rows)["replied"], 1)
        reconcile_events()
        self.assertEqual(delivery_counts(campaign.campaign_delivery_rows)["replied"], 1)

    def test_non_quoted_reply_with_intervening_message_is_not_attributed(self):
        campaign = self.campaign()
        delivery, _, _ = self.evidence(campaign)
        self.outbound(delivery.lead, external_id="wamid.intervening")
        WhatsAppMessage.objects.create(organization=self.org, account=self.account, lead=delivery.lead,
            direction="inbound", from_number=delivery.phone, to_number=self.account.phone_number_id,
            external_id="wamid.nonquoted", body="Thanks", status="received")
        reconcile_events()
        delivery.refresh_from_db()
        self.assertIsNone(delivery.replied_at)

    def test_retry_individual_and_bulk_share_cooldown_and_idempotency(self):
        campaign = self.campaign()
        delivery, _, _ = self.evidence(campaign, code="131049")
        result = retry_recipients(user=self.user, campaign=campaign, selection=campaign.campaign_delivery_rows.all())
        self.assertEqual(result["queued"], 1)
        delivery.refresh_from_db()
        self.assertGreaterEqual(delivery.due_at, delivery.failed_at + timedelta(hours=24))
        again = retry_recipients(user=self.user, campaign=campaign, selection=campaign.campaign_delivery_rows.all())
        self.assertEqual(again["queued"], 0)
        self.assertEqual(delivery.attempts.count(), 1)

    def test_automatic_retry_toggle_is_respected_for_delivery_failures(self):
        for enabled in [False, True]:
            with self.subTest(enabled=enabled):
                campaign = self.campaign(auto_retry=enabled)
                delivery = campaign.campaign_delivery_rows.get()
                message = self.outbound(delivery.lead, external_id=f"wamid.auto.{enabled}")
                CampaignAttempt.objects.create(delivery=delivery, number=1, message=message, provider_id=message.external_id)
                delivery.state, delivery.attempt_count = "accepted", 1
                delivery.save()
                record_status_event(account=self.account, external_id=message.external_id, status="failed",
                    raw_payload={"errors": [{"code": 131049, "title": "Marketing message limited"}]})
                reconcile_events()
                delivery.refresh_from_db()
                self.assertEqual(delivery.state, "pending" if enabled else "failed")

    def test_deleted_lead_does_not_change_frozen_recipient_total(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        frozen = (delivery.name, delivery.phone, delivery.body, delivery.values)
        message = self.outbound(delivery.lead, external_id="wamid.deleted-lead")
        attempt = CampaignAttempt.objects.create(
            delivery=delivery, number=1, message=message,
            provider_id=message.external_id, accepted_at=timezone.now(),
        )
        delivery.lead.delete()
        delivery.refresh_from_db()
        attempt.refresh_from_db()
        self.assertIsNone(delivery.lead_id)
        self.assertIsNone(delivery.recipient_id)
        self.assertEqual((delivery.name, delivery.phone, delivery.body, delivery.values), frozen)
        self.assertEqual(attempt.delivery_id, delivery.pk)
        self.assertEqual(attempt.provider_id, "wamid.deleted-lead")
        self.assertIsNotNone(attempt.accepted_at)
        self.assertIsNone(attempt.message_id)
        self.assertEqual(delivery_counts(campaign.campaign_delivery_rows)["total"], 1)
        self.assertEqual(send_delivery(delivery.pk)["status"], "lead_changed")

    def test_cancel_blocks_future_sends_without_fabricating_delivery(self):
        campaign = self.campaign()
        self.assertEqual(cancel_campaign(user=self.user, campaign=campaign), 1)
        delivery = campaign.campaign_delivery_rows.get()
        with patch.object(whatsapp_service, "send_outbound_message") as sender:
            send_delivery(delivery.pk)
        sender.assert_not_called()
        self.assertEqual(delivery_counts(campaign.campaign_delivery_rows)["skipped"], 1)
        self.assertEqual(delivery_counts(campaign.campaign_delivery_rows)["delivered"], 0)

    def test_changed_template_and_phone_never_send_unreviewed_data(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        self.template.status = "paused"
        self.template.save(update_fields=["status"])
        with patch.object(whatsapp_service, "send_outbound_message") as sender:
            self.assertEqual(send_delivery(delivery.pk)["status"], "configuration_changed")
        sender.assert_not_called()

    def test_legacy_missing_evidence_remains_unknown(self):
        lead = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage, name="Legacy", phone="+919000000001")
        campaign = BulkMessageCampaign.objects.create(organization=self.org, account=self.account, name="Legacy", pipeline=self.pipeline, body="Legacy", status="completed")
        BulkMessageRecipient.objects.create(campaign=campaign, lead=lead, status="sent")
        result = campaign_report(campaign, self.user)
        self.assertIsNone(result["counts"]["replied"])
        self.assertIsNone(result["counts"]["sent"])
        self.assertEqual(result["counts"]["delivered"], 0)
        self.assertFalse(result["can_manage"])

    def test_complete_http_builder_reporting_selection_and_export(self):
        response = self.client.get(self.url("list"))
        self.assertContains(response, "CAMPAIGN STUDIO")
        self.assertContains(response, "Bulk Campaigns")
        metadata = self.client.get(self.url("options")).json()
        self.assertEqual(metadata["accounts"][0]["id"], str(self.account.pk))
        response = self.client.post(self.url("upload"), {"file": SimpleUploadedFile("leads.csv", b"Name,Phone,Email,Company\nAsha,+919000000001,,Example\n")})
        self.assertEqual(response.status_code, 201)
        upload_id = response.json()["id"]
        config = {"mapping": {"name": "Name", "phone": "Phone", "attr:campaign_company": "Company"},
                  "pipeline_id": str(self.pipeline.pk), "stage_id": str(self.stage.pk), "mode": "both"}
        review = self.post("upload-review", config, upload_id=upload_id)
        self.assertEqual(review.status_code, 200)
        templates = self.client.get(self.url("templates"), {"account": str(self.account.pk)}).json()
        self.assertTrue(templates["templates"][0]["supported"])
        preview = self.post("preview", {"upload_id": upload_id, "template_id": str(self.template.pk), "bindings": templates["templates"][0]["bindings"]})
        self.assertTrue(preview.json()["ready"])
        item = CampaignUpload.objects.get(pk=upload_id)
        response = self.post("confirm", self.confirmation(item))
        self.assertEqual(response.status_code, 201)
        campaign_id = response.json()["id"]
        prepare_campaign_chunk(campaign_id)
        report = self.client.get(self.url("detail-data", campaign_id=campaign_id)).json()
        self.assertEqual(report["campaign"]["counts"]["total"], 1)
        recipient_id = report["recipients"][0]["id"]
        details = self.client.get(report["recipients"][0]["lead_url"]).json()
        self.assertEqual(details["name"], "Asha")
        exported = self.post("actions", {"action": "export", "selection": {"ids": [recipient_id]}}, campaign_id=campaign_id)
        text = b"".join(exported.streaming_content).decode("utf-8-sig")
        self.assertIn("Asha", text)
        self.assertIn("'+919000000001", text)
        self.assertNotIn("test-token", text)
        self.assertEqual(self.post("actions", {"action": "rename", "name": "Renamed"}, campaign_id=campaign_id).status_code, 200)
        self.assertEqual(self.post("launch", {}, campaign_id=campaign_id).status_code, 409)
        self.assertEqual(self.client.get(self.url("list-data")).status_code, 200)
        self.assertEqual(self.post("upload-errors", {}, upload_id=upload_id).status_code, 200)
        self.assertEqual(self.client.get(self.url("sample")).status_code, 200)

    def test_cross_campaign_selection_and_cross_tenant_reports_are_rejected(self):
        first, second = self.campaign(), self.campaign()
        foreign_id = str(second.campaign_delivery_rows.get().pk)
        response = self.post("actions", {"action": "export", "selection": {"ids": [foreign_id]}}, campaign_id=first.pk)
        self.assertEqual(response.status_code, 400)
        other = Organization.objects.create(name="Other")
        self.user.organization = other
        self.user.save(update_fields=["organization"])
        self.assertFalse(visible_campaigns(self.user).filter(pk=first.pk).exists())
        self.assertEqual(self.client.get(self.url("detail-data", campaign_id=first.pk)).status_code, 404)

    def test_csrf_and_method_restrictions_are_not_bypassed(self):
        strict = Client(enforce_csrf_checks=True)
        strict.cookies["shvya_crm_sessionid"] = self.session.session_key
        self.assertEqual(strict.post(self.url("confirm"), "{}", content_type="application/json").status_code, 403)
        self.assertEqual(self.client.get(self.url("confirm")).status_code, 405)
        self.assertEqual(self.client.post(self.url("preview"), "[]", content_type="application/json").status_code, 400)

    def test_xlsx_upload_and_unsafe_mapping_validation(self):
        workbook = Workbook()
        workbook.active.append(["Name", "Phone"])
        workbook.active.append(["Spreadsheet", "+919000000002"])
        buffer = BytesIO()
        workbook.save(buffer)
        item = create_upload(user=self.user, uploaded_file=SimpleUploadedFile("audience.xlsx", buffer.getvalue()))
        self.assertEqual(len(item.rows), 1)
        with self.assertRaises(CampaignInputError):
            review_upload(user=self.user, token=item.pk, data={"mapping": {"name": "Name", "phone": "Name"}})
        with self.assertRaises(CampaignInputError):
            create_upload(user=self.user, uploaded_file=None)

    def test_agent_without_edit_permission_cannot_start_or_retry_campaign(self):
        campaign = self.campaign()
        agent = User.objects.create_user(email="campaign-agent@example.com", password="test-password", name="Agent", organization=self.org, role=User.Role.AGENT)
        PipelinePermission.objects.create(user=agent, pipeline=self.pipeline, can_view_pipeline=True, can_edit_leads=False)
        with self.assertRaises(PermissionDenied):
            retry_recipients(user=agent, campaign=campaign, selection=campaign.campaign_delivery_rows.all())

    def test_webhook_gate_rejects_unverified_and_wrong_waba_events(self):
        payload = {"object": "whatsapp_business_account", "entry": [{"id": self.account.waba_id, "changes": [{"value": {
            "metadata": {"phone_number_id": self.account.phone_number_id}, "statuses": [{"id": "wamid.verified", "status": "delivered"}]}}]}]}
        from apps.channels import views_flat
        for status, expected in [(403, 0), (200, 1)]:
            with patch.object(views_flat, "_handle_webhook_delivery", new=lambda request, status=status: HttpResponse(status=status)):
                install_campaign_webhook()
                request = RequestFactory().post("/webhook", json.dumps(payload), content_type="application/json")
                self.assertEqual(views_flat._handle_webhook_delivery(request).status_code, status)
                self.assertEqual(CampaignEvent.objects.filter(external_id="wamid.verified").count(), expected)

    def test_general_worker_schedule_dispatch_and_expiry_maintenance(self):
        from apps.channels.campaign_tasks import dispatch_campaigns_task, maintain_campaigns_task, register_campaign_schedule
        from unittest.mock import Mock
        app = Mock()
        register_campaign_schedule(app)
        self.assertEqual(app.add_periodic_task.call_count, 2)
        campaign = self.campaign()
        with patch("apps.channels.campaign_tasks.send_campaign_recipient_task.apply_async") as sender:
            self.assertEqual(dispatch_campaigns_task.run(), 1)
            sender.assert_called_once()
        unused = self.audience()
        CampaignUpload.objects.filter(pk=unused.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        maintain_campaigns_task.run()
        self.assertFalse(CampaignUpload.objects.filter(pk=unused.pk).exists())
        self.assertTrue(CampaignPlan.objects.filter(campaign=campaign).exists())


class BulkCampaignDeliveryTests(CampaignFixture, TransactionTestCase):
    reset_sequences = True

    def accept(self, *, message):
        self.assertFalse(transaction.get_connection().in_atomic_block)
        self.assertEqual(message.media_payload["transport"], "template")
        self.assertEqual(message.media_payload["template_name"], self.template.name)
        message.status, message.external_id = "sent", f"wamid.test.{message.pk}"
        message.save(update_fields=["status", "external_id", "updated_at"])
        return message

    def test_template_transport_is_outside_locks_and_duplicate_jobs_do_not_resend(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        with patch.object(whatsapp_service, "send_outbound_message", side_effect=self.accept) as provider:
            self.assertEqual(send_delivery(delivery.pk)["status"], "accepted")
            self.assertEqual(send_delivery(delivery.pk)["status"], "not_due")
        provider.assert_called_once()
        self.assertEqual(delivery.attempts.count(), 1)
        counts = delivery_counts(campaign.campaign_delivery_rows)
        self.assertEqual(counts["accepted"], 1)
        self.assertEqual(counts["delivered"], 0)
        self.assertEqual(counts["sent"], 0)

    def test_concurrent_duplicate_worker_cannot_send_same_recipient(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        started, release = Event(), Event()
        def provider(*, message):
            started.set()
            if not release.wait(10):
                raise AssertionError("Test worker was not released")
            return self.accept(message=message)
        def worker():
            close_old_connections()
            try:
                return send_delivery(delivery.pk)
            finally:
                close_old_connections()
        with patch.object(whatsapp_service, "send_outbound_message", side_effect=provider) as sender, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(worker)
            try:
                self.assertTrue(started.wait(10))
                self.assertEqual(send_delivery(delivery.pk)["status"], "not_due")
            finally:
                release.set()
            self.assertEqual(future.result(timeout=15)["status"], "accepted")
        sender.assert_called_once()

    def test_uncertain_provider_result_never_retries_automatically(self):
        campaign = self.campaign(auto_retry=True)
        delivery = campaign.campaign_delivery_rows.get()
        def unknown(*, message):
            raise whatsapp_service.WhatsAppSendError("Timeout") from WhatsAppAPIError("Timeout", status_code=None)
        with patch.object(whatsapp_service, "send_outbound_message", side_effect=unknown):
            self.assertEqual(send_delivery(delivery.pk)["status"], "review")
        delivery.refresh_from_db()
        self.assertTrue(delivery.uncertain)
        result = retry_recipients(user=self.user, campaign=campaign, selection=campaign.campaign_delivery_rows.all())
        self.assertEqual(result["queued"], 0)

    def test_explicit_provider_failure_retries_only_when_enabled(self):
        campaign = self.campaign(auto_retry=True)
        delivery = campaign.campaign_delivery_rows.get()
        def unavailable(*, message):
            message.status = "failed"
            message.raw_payload = {"errors": [{"code": 131016, "title": "Service unavailable"}]}
            message.save(update_fields=["status", "raw_payload", "updated_at"])
            raise whatsapp_service.WhatsAppSendError("Unavailable") from WhatsAppAPIError("Unavailable", status_code=503)
        with patch.object(whatsapp_service, "send_outbound_message", side_effect=unavailable):
            self.assertEqual(send_delivery(delivery.pk)["status"], "pending")
        delivery.refresh_from_db()
        self.assertEqual(delivery.attempt_count, 1)
        self.assertGreater(delivery.due_at, timezone.now())

    def test_missing_provider_id_and_lost_worker_are_review_states(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        with patch.object(whatsapp_service, "send_outbound_message", return_value=None):
            self.assertEqual(send_delivery(delivery.pk)["status"], "review")
        delivery.refresh_from_db()
        delivery.state, delivery.claimed_at = "sending", timezone.now() - timedelta(minutes=20)
        delivery.save(update_fields=["state", "claimed_at"])
        self.assertEqual(recover_expired_claims(), 1)
        delivery.refresh_from_db()
        self.assertEqual(delivery.state, "review")
        self.assertEqual(delivery.error_code, "SHVYA_WORKER_INTERRUPTED")

    def test_sender_gate_defers_without_creating_a_message(self):
        campaign = self.campaign()
        delivery = campaign.campaign_delivery_rows.get()
        CampaignSenderGate.objects.create(account=self.account, next_slot_at=timezone.now() + timedelta(seconds=2))
        with patch.object(whatsapp_service, "send_outbound_message") as provider:
            self.assertEqual(send_delivery(delivery.pk)["status"], "deferred")
        provider.assert_not_called()
        self.assertEqual(delivery.attempts.count(), 0)
