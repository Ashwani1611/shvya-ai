"""Organisation boundaries, filter semantics and actual Message Activity reads."""

from datetime import datetime, timedelta, timezone as dt_timezone
from io import BytesIO
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.db import connection as db_connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from openpyxl import load_workbook

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.models import AITrace
from apps.channels.models import BulkMessageCampaign, WhatsAppAccount, WhatsAppMessage
from apps.channels.campaign_models import CampaignAttempt, CampaignDelivery
from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.followups.models import FollowupExecution, FollowupSequence, FollowupStep, LeadSequenceState
from apps.hosted_automation.models import HostedAutomationJob
from apps.organizations.models import Organization
from apps.triggers.models import SmartTrigger, TriggerEvent, TriggerRun
from apps.superadmin.message_activity import activity_page, get_activity, iter_activity, parse_filters
from apps.superadmin.message_activity_export import ExportTooLarge


class MessageActivityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Activity tenant", timezone="Asia/Kolkata")
        cls.other = Organization.objects.create(name="Private tenant", timezone="UTC")
        cls.admin = User.objects.create_superuser(email="activity-superadmin@example.com", password="test", name="Operations")
        cls.owner = User.objects.create_user(email="activity-owner@example.com", password="test", name="Pipeline owner", organization=cls.org)
        cls.other_owner = User.objects.create_user(email="other-activity@example.com", password="test", name="Private owner", organization=cls.other)
        cls.pipeline = Pipeline.objects.create(organization=cls.org, name="Sales", owner=cls.owner, country_code="91", phone_number="9876543210")
        cls.second_pipeline = Pipeline.objects.create(organization=cls.org, name="Support", country_code="91", phone_number="9876543211")
        cls.other_pipeline = Pipeline.objects.create(organization=cls.other, name="Private pipeline", owner=cls.other_owner)
        cls.stage = Stage.objects.create(pipeline=cls.pipeline, name="New")
        cls.second_stage = Stage.objects.create(pipeline=cls.second_pipeline, name="New")
        cls.other_stage = Stage.objects.create(pipeline=cls.other_pipeline, name="Private stage")
        cls.lead = Lead.objects.create(organization=cls.org, pipeline=cls.pipeline, stage=cls.stage, name="Visible lead", phone="+918169195976", email="visible@example.com")
        cls.second_lead = Lead.objects.create(organization=cls.org, pipeline=cls.second_pipeline, stage=cls.second_stage, name="Second lead", phone="+918169195977")
        cls.other_lead = Lead.objects.create(organization=cls.other, pipeline=cls.other_pipeline, stage=cls.other_stage, name="Private lead", phone="+918169195976")
        cls.account = WhatsAppAccount.objects.create(organization=cls.org, business_name="Sales number", display_phone_number="+919876543210", phone_number_id="123456789012345", connection_type="api")
        cls.hosted = WhatsAppAccount.objects.create(organization=cls.org, business_name="Hosted number", display_phone_number="+919876543211", phone_number_id="919876543211", connection_type="hosted")
        cls.other_account = WhatsAppAccount.objects.create(organization=cls.other, business_name="Private sender", display_phone_number="+919876543212", phone_number_id="123456789012349")
        cls.inbound = cls.message(cls.org, cls.account, cls.lead, "Question about the course", direction="inbound", status="received")
        cls.outbound = cls.message(cls.org, cls.account, cls.lead, "Visible reply", status="delivered")
        cls.second_message = cls.message(cls.org, cls.hosted, cls.second_lead, "Second pipeline content", status="queued")
        cls.private_message = cls.message(cls.other, cls.other_account, cls.other_lead, "PRIVATE MESSAGE")
        cls.sequence = FollowupSequence.objects.create(organization=cls.org, name="Demo follow-up", whatsapp_account=cls.account)
        cls.step = FollowupStep.objects.create(sequence=cls.sequence, position=1, step_type="whatsapp", title="First reminder")
        cls.state = LeadSequenceState.objects.create(organization=cls.org, lead=cls.lead, sequence=cls.sequence, next_step=cls.step, upcoming_send_at=timezone.now() + timedelta(hours=1))
        cls.execution = FollowupExecution.objects.create(organization=cls.org, lead=cls.lead, sequence=cls.sequence, step=cls.step, state=cls.state, scheduled_for=timezone.now(), status="sent", whatsapp_message=cls.outbound, finished_at=timezone.now())
        cls.trace = AITrace.objects.create(organization=cls.org, lead=cls.lead, pipeline_id=cls.pipeline.pk, stage_id=cls.stage.pk, whatsapp_account_id=cls.account.pk, source_inbound_message_id=cls.inbound.pk, outbound_message_id=cls.outbound.pk, connection_type="api", status="completed", response_preview="Visible reply preview", model_name="test-model", completed_at=timezone.now(), details={"raw_secret": "DO-NOT-EXPOSE-TRACE-DETAILS"})
        cls.hosted_inbound = cls.message(cls.org, cls.hosted, cls.second_lead, "Hosted inbound", direction="inbound", status="received")
        cls.job = HostedAutomationJob.objects.create(organization=cls.org, account=cls.hosted, lead=cls.second_lead, source_message=cls.hosted_inbound, available_at=timezone.now() + timedelta(minutes=1), status="queued")

    @staticmethod
    def message(org, account, lead, body, *, direction="outbound", status="sent"):
        return WhatsAppMessage.objects.create(
            organization=org, account=account, lead=lead, direction=direction, status=status, body=body,
            from_number=lead.phone if direction == "inbound" else account.display_phone_number,
            to_number=account.display_phone_number if direction == "inbound" else lead.phone,
        )

    def setUp(self):
        self.url = reverse("superadmin-organization-message-activity", args=[self.org.pk])
        self.export_url = reverse("superadmin-organization-message-activity-export", args=[self.org.pk])
        self.login_as(self.admin)

    def login_as(self, user):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

    def detail_url(self, kind, record_id):
        return reverse("superadmin-organization-message-activity-detail", args=[self.org.pk, kind, str(record_id)])

    def rows(self, **params):
        return list(iter_activity(self.org, parse_filters(self.org, params)))

    def test_superadmin_session_required_on_all_entry_points(self):
        routes = [self.url, self.export_url, self.detail_url("whatsapp", self.outbound.pk)]
        for user in (None, self.owner):
            self.client.cookies.clear()
            if user:
                self.login_as(user)
            for route in routes:
                with self.subTest(user=user and user.pk, route=route):
                    self.assertEqual(self.client.get(route).status_code, 302)
        self.login_as(self.admin)
        for route in routes:
            self.assertEqual(self.client.post(route).status_code, 405)

    def test_page_shows_separate_messages_ai_and_cadence_without_other_tenant(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertContains(response, "Message Activity")
        self.assertContains(response, "Visible lead")
        for private in ("PRIVATE MESSAGE", "Private pipeline", "Private sender", "DO-NOT-EXPOSE-TRACE-DETAILS"):
            self.assertNotContains(response, private)
        rows = response.context["activity"]["rows"]
        self.assertTrue({"whatsapp", "ai", "cadence", "sequence", "queue"}.issubset({row["kind"] for row in rows}))
        self.assertEqual(next(row for row in rows if row["kind"] == "ai")["status"], "completed")
        self.assertEqual(next(row for row in rows if row["id"] == str(self.outbound.pk))["status"], "delivered")

    def test_foreign_and_malformed_filters_fail_closed(self):
        other_sequence = FollowupSequence.objects.create(organization=self.other, name="Private sequence", whatsapp_account=self.other_account)
        cases = [
            {"pipeline": str(self.other_pipeline.pk)}, {"connection": f"wa:{self.other_account.pk}"},
            {"owner": str(self.other_owner.pk)}, {"sequence": str(other_sequence.pk)},
            {"pipeline": "not-a-uuid"}, {"connection": "bogus:invalid"}, {"kind": "unsupported"},
            {"outcome": "unsupported"}, {"direction": "unsupported"}, {"from": "2026-55-99"},
            {"from": "2026-10-10", "to": "2026-10-01"}, {"from": "2024-01-01", "to": "2026-10-01"},
            {"from": "0001-01-01", "to": "0001-01-01"},
            {"q": "bad\x00search"},
            {"page": "-1"},
        ]
        for params in cases:
            with self.subTest(params=params):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 400)
                self.assertNotContains(response, "Visible reply", status_code=400)
                self.assertNotContains(response, "PRIVATE MESSAGE", status_code=400)
        response = self.client.get(self.export_url, {"connection": f"wa:{self.other_account.pk}"})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("Content-Disposition", response)

    def test_pipeline_sender_owner_and_direction_filters_match_actual_records(self):
        params = {"kind": "whatsapp", "pipeline": str(self.pipeline.pk), "connection": f"wa:{self.account.pk}", "owner": str(self.owner.pk), "direction": "outbound"}
        rows = self.rows(**params)
        self.assertEqual([row["id"] for row in rows], [str(self.outbound.pk)])
        self.assertEqual(rows[0]["from_number"], "+919876543210")
        self.assertNotEqual(rows[0]["from_number"], self.account.phone_number_id)
        # Disconnecting an account does not erase its historical messages.
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(is_active=False, status="disconnected")
        self.assertEqual([row["id"] for row in self.rows(**params)], [str(self.outbound.pk)])

    def test_search_content_and_lead_fields_use_same_filters(self):
        self.assertEqual([row["id"] for row in self.rows(kind="whatsapp", q="Visible reply")], [str(self.outbound.pk)])
        self.assertEqual({row["id"] for row in self.rows(kind="whatsapp", q="8169195976")}, {str(self.outbound.pk), str(self.inbound.pk)})
        self.assertEqual(self.rows(kind="whatsapp", q="PRIVATE MESSAGE"), [])

    def test_date_range_is_org_local_and_receipt_updates_do_not_move_records(self):
        at_midnight = datetime(2026, 10, 1, 18, 30, tzinfo=dt_timezone.utc)
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(created_at=at_midnight, sent_at=at_midnight + timedelta(minutes=2))
        WhatsAppMessage.objects.filter(pk=self.inbound.pk).update(created_at=at_midnight - timedelta(seconds=1))
        rows = self.rows(kind="whatsapp", **{"from": "2026-10-02", "to": "2026-10-02"})
        self.assertEqual([row["id"] for row in rows], [str(self.outbound.pk)])
        self.assertEqual(rows[0]["sent_at"], at_midnight + timedelta(minutes=2))

    def test_current_pipeline_filter_preserves_original_message_sender(self):
        Lead.objects.filter(pk=self.lead.pk).update(pipeline=self.second_pipeline, stage=self.second_stage)
        rows = self.rows(kind="whatsapp", pipeline=str(self.second_pipeline.pk), connection=f"wa:{self.account.pk}")
        self.assertEqual({row["id"] for row in rows}, {str(self.inbound.pk), str(self.outbound.pk)})
        self.assertTrue(all(row["pipeline_name"] == "Support" for row in rows))
        self.assertTrue(all(row["connection_id"] == f"wa:{self.account.pk}" for row in rows))

    def test_cross_tenant_related_rows_are_excluded_even_with_inconsistent_legacy_fks(self):
        corrupt_account = self.message(self.org, self.other_account, self.lead, "CROSS ACCOUNT SECRET")
        corrupt_lead = self.message(self.org, self.account, self.other_lead, "CROSS LEAD SECRET")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "CROSS ACCOUNT SECRET")
        self.assertNotContains(response, "CROSS LEAD SECRET")
        for message in (corrupt_account, corrupt_lead, self.private_message):
            self.assertEqual(self.client.get(self.detail_url("whatsapp", message.pk)).status_code, 404)

    def test_detail_links_only_exact_org_owned_ai_and_cadence_records(self):
        row = get_activity(self.org, "ai", str(self.trace.pk))
        self.assertEqual(row["status"], "completed")
        self.assertTrue(row["content_is_preview"])
        self.assertIn(str(self.outbound.pk), {str(item["id"]) for item in row["related"]})
        response = self.client.get(self.detail_url("whatsapp", self.outbound.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible reply")
        self.assertNotContains(response, "DO-NOT-EXPOSE-TRACE-DETAILS")
        AITrace.objects.filter(pk=self.trace.pk).update(outbound_message_id=self.private_message.pk, pipeline_id=self.other_pipeline.pk, stage_id=self.other_stage.pk)
        response = self.client.get(self.detail_url("ai", self.trace.pk))
        self.assertIn(response.status_code, (200, 404))
        self.assertNotIn(b"PRIVATE MESSAGE", response.content)
        self.assertNotIn(b"Private pipeline", response.content)

    def test_unlinked_inbound_remains_visible_by_receiving_connection(self):
        message = WhatsAppMessage.objects.create(organization=self.org, account=self.account, direction="inbound", from_number="919888777666", to_number="919876543210", body="No lead yet", status="received")
        rows = self.rows(kind="whatsapp", connection=f"wa:{self.account.pk}")
        self.assertIn(str(message.pk), {row["id"] for row in rows})
        self.assertNotIn(str(message.pk), {row["id"] for row in self.rows(kind="whatsapp", pipeline=str(self.pipeline.pk))})

    def test_instagram_message_needs_no_phone_and_has_verified_account_filter(self):
        account = InstagramAccount.objects.create(organization=self.org, ig_user_id="1111222233334444", username="activity_brand")
        conversation = InstagramConversation.objects.create(organization=self.org, account=account, participant_id="9999999999999999", participant_username="instagram_lead")
        message = InstagramMessage.objects.create(organization=self.org, account=account, conversation=conversation, direction="inbound", status="received", sender_id=conversation.participant_id, recipient_id=account.ig_user_id, body="Instagram enquiry")
        rows = self.rows(kind="instagram", connection=f"ig:{account.pk}")
        self.assertEqual([row["id"] for row in rows], [str(message.pk)])
        self.assertEqual(rows[0]["lead_phone"], "")
        self.assertEqual(self.client.get(self.detail_url("instagram", message.pk)).status_code, 200)

    def test_queue_is_not_a_sent_message_and_retains_scheduled_time(self):
        row = get_activity(self.org, "queue", f"hosted:{self.job.pk}")
        self.assertEqual(row["status"], "queued")
        self.assertIsNone(row["sent_at"])
        self.assertEqual(row["scheduled_at"], self.job.available_at)
        self.assertEqual(row["connection_id"], f"wa:{self.hosted.pk}")

    def test_durable_api_and_instagram_queue_states_are_filterable_without_claiming_delivery(self):
        recorded_at = timezone.now()
        metadata = {"shvya_ai_execution": {"status": "retrying", "attempts": 2, "reason": "provider_backoff", "updated_at": recorded_at.isoformat()}}
        WhatsAppMessage.objects.filter(pk=self.inbound.pk).update(raw_payload=metadata)
        account = InstagramAccount.objects.create(organization=self.org, ig_user_id="queue-ig-account", username="queue_brand")
        conversation = InstagramConversation.objects.create(organization=self.org, account=account, lead=self.lead, participant_id="queue-participant")
        message = InstagramMessage.objects.create(organization=self.org, account=account, conversation=conversation, direction="inbound", status="received", body="Queue this enquiry", raw_payload=metadata)
        for prefix, record, connection in (("wa", self.inbound, self.account), ("ig", message, account)):
            with self.subTest(prefix=prefix):
                rows = self.rows(kind="queue", connection=f"{prefix}:{connection.pk}", outcome="waiting")
                self.assertEqual([row["id"] for row in rows], [f"{prefix}:{record.pk}"])
                self.assertEqual(rows[0]["status"], "retrying")
                self.assertEqual(rows[0]["attempt"], 2)
                self.assertEqual(rows[0]["updated_at"], recorded_at)
                self.assertIsNone(rows[0]["sent_at"])
                self.assertEqual(self.client.get(self.detail_url("queue", f"{prefix}:{record.pk}")).status_code, 200)

    def test_instagram_sequence_filter_uses_exact_saved_message_reference(self):
        account = InstagramAccount.objects.create(organization=self.org, ig_user_id="sequence-ig-account", username="sequence_brand")
        conversation = InstagramConversation.objects.create(organization=self.org, account=account, lead=self.second_lead, participant_id="sequence-participant")
        message = InstagramMessage.objects.create(organization=self.org, account=account, conversation=conversation, direction="outbound", status="sent", body="Instagram Cadence message")
        sequence = FollowupSequence.objects.create(organization=self.org, name="Instagram follow-up", instagram_account=account)
        step = FollowupStep.objects.create(sequence=sequence, step_type="instagram", position=1)
        state = LeadSequenceState.objects.create(organization=self.org, lead=self.second_lead, sequence=sequence, next_step=step)
        execution = FollowupExecution.objects.create(organization=self.org, lead=self.second_lead, sequence=sequence, step=step, state=state, status="sent", scheduled_for=timezone.now(), payload={"instagram_message_id": str(message.pk)})
        rows = self.rows(kind="instagram", sequence=str(sequence.pk), connection=f"ig:{account.pk}")
        self.assertEqual([row["id"] for row in rows], [str(message.pk)])
        self.assertEqual(rows[0]["sequence_name"], sequence.name)
        detail = get_activity(self.org, "cadence", str(execution.pk))
        self.assertEqual(detail["content"], "Instagram Cadence message")
        self.assertIn(str(message.pk), {row["id"] for row in detail["related"]})
        sequence.instagram_account = None
        sequence.whatsapp_account = self.account
        sequence.save(update_fields=["instagram_account", "whatsapp_account"])
        # Changing future routing must not erase a retained message's sender or
        # its exact execution relationship. Each org has one Instagram account.
        for kind, identifier in (("instagram", message.pk), ("cadence", execution.pk)):
            with self.subTest(historical_kind=kind):
                rows = self.rows(kind=kind, sequence=str(sequence.pk), connection=f"ig:{account.pk}")
                self.assertEqual([row["id"] for row in rows], [str(identifier)])
                self.assertEqual(rows[0]["connection_id"], f"ig:{account.pk}")
        detail = get_activity(self.org, "cadence", str(execution.pk))
        self.assertEqual(detail["content"], "Instagram Cadence message")
        self.assertEqual(detail["connection_id"], f"ig:{account.pk}")
        self.assertIn(str(execution.pk), {item["id"] for item in get_activity(self.org, "instagram", str(message.pk))["related"]})

    def test_operations_test_leads_are_excluded_from_messages_and_automation(self):
        Lead.objects.filter(pk=self.lead.pk).update(is_operations_test=True)
        rows = self.rows()
        self.assertNotIn(str(self.lead.pk), {row["lead_id"] for row in rows})
        for kind, identifier in (("whatsapp", self.outbound.pk), ("ai", self.trace.pk), ("cadence", self.execution.pk), ("sequence", self.state.pk)):
            with self.subTest(kind=kind):
                self.assertEqual(self.client.get(self.detail_url(kind, identifier)).status_code, 404)

    def test_larger_pages_keep_queries_batched_and_reads_do_not_mutate_records(self):
        WhatsAppMessage.objects.bulk_create([
            WhatsAppMessage(organization=self.org, account=self.account, lead=self.lead,
                            direction="outbound", status="sent", body=f"Batched activity {index}")
            for index in range(50)
        ])
        filters = parse_filters(self.org, {"kind": "whatsapp", "q": "Batched activity"})
        counts = []
        for size in (1, 50):
            with CaptureQueriesContext(db_connection) as captured:
                result = activity_page(self.org, filters, per_page=size)
            self.assertEqual(len(result["rows"]), size)
            self.assertTrue(all(query["sql"].lstrip().split()[0].upper() in {"SELECT", "DECLARE", "FETCH", "CLOSE"} for query in captured))
            counts.append(len(captured))
        self.assertEqual(counts[0], counts[1], "A larger page must not add per-record queries.")

    def test_recorded_time_does_not_change_when_provider_send_crosses_midnight(self):
        created = datetime(2026, 10, 1, 18, 29, tzinfo=dt_timezone.utc)
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(created_at=created, sent_at=created + timedelta(minutes=3))
        rows = self.rows(kind="whatsapp", **{"from": "2026-10-01", "to": "2026-10-01"})
        self.assertEqual([row["id"] for row in rows], [str(self.outbound.pk)])
        self.assertEqual(rows[0]["event_time"], created)

    def test_current_connection_rename_does_not_replace_stored_sender_identity(self):
        WhatsAppAccount.objects.filter(pk=self.account.pk).update(display_phone_number="+919900001111")
        row = get_activity(self.org, "whatsapp", str(self.outbound.pk))
        self.assertEqual(row["from_number"], "+919876543210")
        self.assertEqual(row["to_number"], self.lead.phone)

    def test_workflow_message_and_pre_send_block_are_inspectable(self):
        rule = SmartTrigger.objects.create(organization=self.org, name="Stage response", trigger_type="stage_change", action_type="message", fingerprint="activity-workflow", action={"account": str(self.account.pk)})
        event = TriggerEvent.objects.create(organization=self.org, lead=self.lead, kind="stage_change", key="activity-event")
        run = TriggerRun.objects.create(rule=rule, event=event, lead=self.lead, action_type="message", action={"account": str(self.account.pk)}, status="blocked", detail="Lead AI disabled", due_at=timezone.now())
        rows = self.rows(kind="workflow", connection=f"wa:{self.account.pk}", outcome="attention")
        self.assertEqual([row["id"] for row in rows], [str(run.pk)])
        self.assertIsNone(rows[0]["sent_at"])
        self.assertEqual(self.client.get(self.detail_url("workflow", run.pk)).status_code, 200)
        TriggerRun.objects.filter(pk=run.pk).update(status="completed", message=self.outbound, finished_at=timezone.now())
        row = get_activity(self.org, "workflow", str(run.pk))
        self.assertEqual(row["content"], "Visible reply")
        self.assertIn(str(self.outbound.pk), {item["id"] for item in row["related"]})

    def test_queued_ai_delivery_pacing_remains_visible(self):
        due = timezone.now() + timedelta(minutes=3)
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(status="queued", raw_payload={"shvya_ai_delivery": {"available_at": due.isoformat(), "reason": "sender_pacing"}})
        row = get_activity(self.org, "whatsapp", str(self.outbound.pk))
        self.assertEqual(row["scheduled_at"], due)
        self.assertIn("sender_pacing", str(row["history"]) + row["error"])

    def test_campaign_message_preserves_receipt_timestamps(self):
        message = self.message(self.org, self.account, self.lead, "Campaign message", status="read")
        campaign = BulkMessageCampaign.objects.create(organization=self.org, account=self.account, pipeline=self.pipeline, name="Course launch", body="Campaign message")
        delivery = CampaignDelivery.objects.create(campaign=campaign, lead=self.lead, name=self.lead.name, phone=self.lead.phone, pipeline_key=self.pipeline.pk, stage_key=self.stage.pk, pipeline_label=self.pipeline.name, stage_label=self.stage.name, body="Campaign message", state="accepted")
        delivered_at = timezone.now() - timedelta(minutes=3)
        read_at = timezone.now() - timedelta(minutes=1)
        CampaignAttempt.objects.create(delivery=delivery, number=1, message=message, delivered_at=delivered_at, read_at=read_at)
        row = get_activity(self.org, "whatsapp", str(message.pk))
        self.assertIn("campaign", row["origin"].lower())
        self.assertIn(delivered_at, {item["time"] for item in row["history"]})
        self.assertIn(read_at, {item["time"] for item in row["history"]})

    def test_stable_pagination_no_duplicates_and_all_records_exported(self):
        now = timezone.now()
        messages = [WhatsAppMessage(organization=self.org, account=self.account, lead=self.lead, direction="outbound", status="queued", body=f"Paged {i}", from_number="919876543210", to_number="918169195976") for i in range(57)]
        WhatsAppMessage.objects.bulk_create(messages)
        WhatsAppMessage.objects.filter(pk__in=[message.pk for message in messages]).update(created_at=now)
        filters = parse_filters(self.org, {"kind": "whatsapp", "q": "Paged"})
        first = activity_page(self.org, filters, page=1, per_page=50)
        second = activity_page(self.org, filters, page=2, per_page=50)
        self.assertEqual(first["total"], 57)
        self.assertEqual(len(first["rows"]), 50)
        self.assertEqual(len(second["rows"]), 7)
        self.assertEqual(len({row["id"] for row in first["rows"] + second["rows"]}), 57)
        self.assertEqual([row["id"] for row in iter_activity(self.org, filters)], [row["id"] for row in first["rows"] + second["rows"]])

    def test_export_ignores_page_but_preserves_all_applied_filters(self):
        response = self.client.get(self.export_url, {"kind": "whatsapp", "connection": f"wa:{self.account.pk}", "page": "2"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Disposition"].endswith('.xlsx"'))
        contents = b"".join(response.streaming_content)
        # Django's test-client iterator closes the response while preserving the
        # TestCase transaction. Calling close() again emits request_finished a
        # second time and incorrectly closes that active test connection.
        self.assertTrue(response.closed)
        workbook = load_workbook(BytesIO(contents), read_only=True)
        sheet = workbook["Message Activity"]
        values = list(sheet.values)
        self.assertEqual(len(values), 3)
        columns = {name: index for index, name in enumerate(values[0])}
        self.assertTrue(all(row[columns["Lead number"]] == "918169195976" for row in values[1:]))
        self.assertNotIn("PRIVATE MESSAGE", str(values))
        self.assertNotIn("Second pipeline content", str(values))
        workbook.close()

    def test_export_limit_reports_failure_instead_of_silent_truncation(self):
        with patch("apps.superadmin.message_activity_views.MAX_EXPORT_ROWS", 1):
            response = self.client.get(self.export_url, {"kind": "whatsapp"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("Content-Disposition", response)

    def test_export_failure_closes_its_owned_row_iterator(self):
        finished = []

        def rows(*args, **kwargs):
            try:
                yield {"id": "temporary-row"}
                yield {"id": "not-reached"}
            finally:
                finished.append(True)

        def fail_writer(*args, **kwargs):
            next(kwargs["rows"])
            raise ExportTooLarge("Recorded History exceeds Excel's row limit. Narrow filters.")

        with patch("apps.superadmin.message_activity_views.iter_activity", side_effect=rows), patch(
            "apps.superadmin.message_activity_views.write_activity_workbook", side_effect=fail_writer,
        ):
            response = self.client.get(self.export_url)
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("Content-Disposition", response)
        self.assertEqual(finished, [True])
        self.assertContains(response, "Narrow", status_code=422)

    def test_message_html_and_sensitive_diagnostic_errors_render_as_plain_text(self):
        WhatsAppMessage.objects.filter(pk=self.outbound.pk).update(body='<script>window.bad=true</script> Message content', error="Provider failure Bearer activity-secret-token")
        response = self.client.get(self.detail_url("whatsapp", self.outbound.pk))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;script&gt;")
        self.assertNotContains(response, "<script>window.bad")
        self.assertNotContains(response, "activity-secret-token")
