from datetime import timedelta

from django.contrib.sessions.backends.db import SessionStore
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import AttributeDefinition, Lead, LeadReminder, Pipeline, Stage
from apps.telephony.models import CallDevice, CallRecord, CallIntelligenceSettings
from apps.telephony.services import ingest_call_event, register_device
from . import test_call_intelligence as fixtures


class MobileWorkspaceTests(TestCase):
    setUp = fixtures.CallIntelligenceTests.setUp
    payload = fixtures.CallIntelligenceTests.payload

    def api(self, user=None):
        client = APIClient()
        client.force_authenticate(user=user or self.user)
        return client

    def web(self, user=None):
        session = SessionStore()
        set_authenticated_user(session, user or self.user)
        session.save()
        client = Client()
        client.cookies[get_session_cookie_name("dashboard")] = session.session_key
        return client

    def call(self, **kwargs):
        return ingest_call_event(user=self.user, payload=self.payload(**kwargs))["call"]

    def test_dashboard_filters_apply_to_metrics_and_history(self):
        self.call(status="answered")
        self.call(status="missed", talk_duration_seconds=0, ring_duration_seconds=20,
                  ended_at=(timezone.now() - timedelta(days=2)).isoformat())
        response = self.web().get("/dashboard/call-intelligence/", {"section": "analytics", "status": "missed"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertEqual(response.context["stats"]["total_ring"], 20)
        self.assertEqual(len(response.context["recent_calls"]), 1)
        self.assertContains(response, "Rang: 20 sec")
        self.assertContains(response, "shvya_premium_sidebar_mobile.css")
        self.assertNotContains(response, "Download for Android")

    def test_date_filter_updates_metrics_and_activity_together(self):
        self.call(ring_duration_seconds=8)
        self.call(ring_duration_seconds=30, ended_at=(timezone.now() - timedelta(days=3)).isoformat())
        response = self.web().get("/dashboard/call-intelligence/", {
            "date_from": timezone.localdate().isoformat(), "section": "analytics",
        })
        self.assertEqual(response.context["stats"]["total"], 1)
        self.assertEqual(response.context["stats"]["total_ring"], 8)
        self.assertEqual(len(response.context["recent_calls"]), 1)

    def test_unknown_ringing_not_reported_as_zero_measurement(self):
        self.call(ring_duration_seconds=0)
        response = self.web().get("/dashboard/call-intelligence/?section=analytics")
        self.assertEqual(response.context["stats"]["measured_ring"], 0)
        self.assertContains(response, "Rang: Not available")

    def test_remove_device_stops_automatic_registration_and_ingestion(self):
        call = self.call()
        device = CallDevice.objects.get(device_id="android-001")
        response = self.web().post(f"/dashboard/call-intelligence/devices/{device.id}/remove/")
        self.assertEqual(response.status_code, 302)
        api = self.api()
        response = api.post("/api/v1/call-intelligence/devices/register/", {"device_id": device.device_id}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["code"], "device_removed")
        response = api.post("/api/v1/call-intelligence/events/", self.payload(), format="json")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(CallRecord.objects.filter(pk=call.pk).exists())
        self.assertEqual(CallRecord.objects.count(), 1)

    def test_device_removal_is_post_only_and_tenant_scoped(self):
        device = CallDevice.objects.get(device_id="android-001")
        url = f"/dashboard/call-intelligence/devices/{device.id}/remove/"
        self.assertEqual(self.web().get(url).status_code, 405)
        self.assertEqual(self.web(self.other_user).post(url).status_code, 404)
        device.refresh_from_db()
        self.assertTrue(device.is_active)

    def test_agent_cannot_remove_another_employees_device(self):
        self.user.role = User.Role.AGENT
        self.user.save(update_fields=["role"])
        peer = User.objects.create_user(email="peer@example.com", organization=self.org, password="secret123")
        device = register_device(user=peer, payload={"device_id": "peer-device"})
        self.assertEqual(self.web().post(f"/dashboard/call-intelligence/devices/{device.id}/remove/").status_code, 404)
        self.assertNotContains(self.web().get("/dashboard/call-intelligence/"), "peer-device")

    def test_mobile_call_summary_is_filtered_and_scoped(self):
        self.call()
        self.call(status="missed", talk_duration_seconds=0)
        response = self.api().get("/api/v1/call-intelligence/calls/?mine=1&status=missed")
        self.assertEqual(response.data["stats"]["total"], 1)
        self.assertEqual(response.data["stats"]["not_picked"], 1)
        self.assertEqual(len(response.data["calls"]), 1)
        self.assertEqual(self.api(self.other_user).get("/api/v1/call-intelligence/calls/").data["stats"]["total"], 0)

    def test_mobile_pagination_keeps_full_summary(self):
        call = self.call()
        now = timezone.now()
        CallRecord.objects.bulk_create([CallRecord(
            organization=self.org, user=self.user, phone_number=f"+91999999{i:04}",
            source="cloud", source_call_id=f"page-{i}", direction="incoming", status="missed", ended_at=now,
        ) for i in range(51)])
        response = self.api().get("/api/v1/call-intelligence/calls/?mine=1")
        self.assertEqual(len(response.data["calls"]), 50)
        self.assertEqual(response.data["stats"]["total"], 52)
        self.assertTrue(response.data["has_next"])
        second = self.api().get("/api/v1/call-intelligence/calls/?mine=1&page=2")
        self.assertEqual(len(second.data["calls"]), 2)
        self.assertFalse(second.data["has_next"])
        self.assertTrue(CallRecord.objects.filter(pk=call.pk).exists())

    def test_mobile_notes_update_crm_without_erasing_disposition(self):
        call = self.call()
        call.disposition = "interested"
        call.save(update_fields=["disposition"])
        response = self.api().patch(
            f"/api/v1/call-intelligence/calls/{call.id}/notes/",
            {"notes": "Asked for a follow-up next week."}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.notes, "Asked for a follow-up next week.")
        self.assertEqual(call.disposition, "interested")
        self.assertEqual(call.crm_call.notes, call.notes)

    def reminder(self):
        self.pipeline.owner = self.user
        self.pipeline.save(update_fields=["owner", "updated_at"])
        call = self.call()
        call.follow_up_required = True
        call.save(update_fields=["follow_up_required"])
        row = LeadReminder.objects.create(lead=call.lead, assigned_to=self.user, title="Call back",
                                         due_at=timezone.now() - timedelta(days=2))
        return call, row

    def test_mobile_reminders_follow_accessible_pipeline_not_assignee(self):
        call, row = self.reminder()
        peer = User.objects.create_user(email="reminder-peer@example.com", organization=self.org, password="secret123")
        row.assigned_to = peer
        row.save(update_fields=["assigned_to", "updated_at"])
        response = self.api().get("/api/v1/call-intelligence/reminders/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r["id"] for r in response.data["reminders"]], [str(row.id)])
        self.assertEqual(response.data["stats"]["overdue"], 1)
        self.assertEqual(self.api().post(
            f"/api/v1/call-intelligence/reminders/{row.id}/action/", {"action": "snooze"}, format="json",
        ).status_code, 200)

        other_pipeline = Pipeline.objects.create(organization=self.org, name="Private pipeline", owner=peer)
        other_stage = Stage.objects.create(pipeline=other_pipeline, name="New")
        hidden_lead = Lead.objects.create(organization=self.org, pipeline=other_pipeline, stage=other_stage,
                                          name="Hidden lead", phone="+919800000001")
        hidden = LeadReminder.objects.create(lead=hidden_lead, assigned_to=self.user, title="Hidden reminder",
                                             due_at=timezone.now() + timedelta(days=1))
        response = self.api().get("/api/v1/call-intelligence/reminders/")
        self.assertEqual(response.data["stats"]["total"], 1)
        self.assertEqual(self.api().post(
            f"/api/v1/call-intelligence/reminders/{hidden.id}/action/", {"action": "delete"}, format="json",
        ).status_code, 404)
        self.assertTrue(LeadReminder.objects.filter(pk=hidden.pk).exists())

    def test_reminder_counts_do_not_count_overdue_today_twice(self):
        _, row = self.reminder()
        row.due_at = timezone.now() - timedelta(minutes=5)
        row.save(update_fields=["due_at", "updated_at"])
        stats = self.api().get("/api/v1/call-intelligence/reminders/").data["stats"]
        self.assertEqual((stats["total"], stats["overdue"], stats["today"]), (1, 1, 0))

    def test_reminder_snooze_uses_now_for_overdue_and_updates_call(self):
        call, row = self.reminder()
        response = self.api().post(f"/api/v1/call-intelligence/reminders/{row.id}/action/", {"action": "snooze"}, format="json")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        call.refresh_from_db()
        self.assertGreater(row.due_at, timezone.now() + timedelta(minutes=29))
        self.assertEqual(call.follow_up_at, row.due_at)

    def test_reminder_complete_updates_canonical_reminder_and_call(self):
        call, row = self.reminder()
        response = self.api().post(f"/api/v1/call-intelligence/reminders/{row.id}/action/", {"action": "complete"}, format="json")
        self.assertEqual(response.status_code, 200)
        row.refresh_from_db()
        call.refresh_from_db()
        self.assertEqual(row.status, "completed")
        self.assertFalse(call.follow_up_required)
        self.assertEqual(self.api().get("/api/v1/call-intelligence/reminders/").data["stats"]["total"], 0)

    def test_reminder_delete_and_cross_tenant_denial(self):
        _, row = self.reminder()
        url = f"/api/v1/call-intelligence/reminders/{row.id}/action/"
        self.assertEqual(self.api(self.other_user).get("/api/v1/call-intelligence/reminders/").data["stats"]["total"], 0)
        self.assertEqual(self.api(self.other_user).post(url, {"action": "delete"}, format="json").status_code, 404)
        self.assertEqual(self.api().post(url, {"action": "delete"}, format="json").status_code, 200)
        self.assertFalse(LeadReminder.objects.filter(pk=row.id).exists())

    def test_settings_patch_requires_admin_and_preserves_other_rules(self):
        self.user.role = User.Role.AGENT
        self.user.save(update_fields=["role"])
        url = "/api/v1/call-intelligence/settings/"
        self.assertEqual(self.api().patch(url, {"auto_create_missed": False}, format="json").status_code, 403)
        self.user.role = User.Role.ADMIN
        self.user.save(update_fields=["role"])
        settings = CallIntelligenceSettings.objects.get(organization=self.org)
        before = settings.auto_create_answered_incoming
        self.assertEqual(self.api().patch(url, {"auto_create_missed": False}, format="json").status_code, 200)
        settings.refresh_from_db()
        self.assertFalse(settings.auto_create_missed)
        self.assertEqual(settings.auto_create_answered_incoming, before)
        self.assertEqual(self.api().patch(url, {"auto_create_missed": "false"}, format="json").status_code, 400)

    def test_add_lead_preserves_existing_crm_route(self):
        call = self.call()
        url = "/api/v1/call-intelligence/leads/"
        response = self.api().post(url, {"name": "Replacement", "phone": call.phone_number}, format="json")
        self.assertEqual(response.status_code, 409)
        call.lead.refresh_from_db()
        self.assertNotEqual(call.lead.name, "Replacement")
        response = self.api().post(url, {"name": "New prospect", "phone": "9123456789"}, format="json")
        self.assertEqual(response.status_code, 201)

    def test_mobile_lead_form_exposes_organization_fields_without_descriptions(self):
        AttributeDefinition.objects.create(
            organization=self.org, key="interest", name="Interest", field_type="option",
            options=["CRM", "Calls"], description="Internal guidance",
        )
        self.user.role = User.Role.ADMIN
        self.user.save(update_fields=["role"])
        response = self.api().get("/api/v1/call-intelligence/leads/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["organization_name"], self.org.name)
        self.assertEqual(response.data["attributes"][0]["options"], ["CRM", "Calls"])
        self.assertNotIn("description", response.data["attributes"][0])
        self.assertIn(str(self.pipeline.id), [row["id"] for row in response.data["pipelines"]])
        other = self.api(self.other_user).get("/api/v1/call-intelligence/leads/")
        self.assertEqual(other.status_code, 200)
        self.assertFalse(any(row["key"] == "interest" for row in other.data["attributes"]))

    def test_mobile_lead_form_saves_details_in_selected_organization_pipeline(self):
        self.user.role = User.Role.ADMIN
        self.user.save(update_fields=["role"])
        AttributeDefinition.objects.create(
            organization=self.org, key="interest", name="Interest", field_type="option",
            options=["CRM", "Calls"],
        )
        url = "/api/v1/call-intelligence/leads/"
        payload = {
            "name": "Mobile prospect", "phone": "9876501234", "email": "lead@example.com",
            "notes": "Requested a demo", "pipeline_id": str(self.pipeline.id),
            "stage_id": str(self.stage.id), "attributes": {"interest": "Calls"},
        }
        response = self.api().post(url, payload, format="json")
        self.assertEqual(response.status_code, 201)
        lead = Lead.objects.get(pk=response.data["lead_id"])
        self.assertEqual((lead.pipeline, lead.stage), (self.pipeline, self.stage))
        self.assertEqual((lead.email, lead.notes, lead.attributes["interest"]),
                         ("lead@example.com", "Requested a demo", "Calls"))
        self.assertEqual(lead.lead_source, "system")

        payload["phone"] = "9876501235"
        payload["attributes"] = {"interest": "Not an option"}
        self.assertEqual(self.api().post(url, payload, format="json").status_code, 400)

        other_pipeline = Pipeline.objects.get(organization=self.other_org, name="Leads")
        other_stage = Stage.objects.filter(pipeline=other_pipeline).first()
        payload["attributes"] = {}
        payload["pipeline_id"] = str(other_pipeline.id)
        payload["stage_id"] = str(other_stage.id)
        self.assertEqual(self.api().post(url, payload, format="json").status_code, 400)

    def test_mobile_lead_detail_returns_only_human_readable_configured_attributes(self):
        AttributeDefinition.objects.create(
            organization=self.org,
            key="interest",
            name="Product interest",
            field_type="text",
            display_order=1,
        )
        AttributeDefinition.objects.create(
            organization=self.org,
            key="budget",
            name="Budget",
            field_type="text",
            display_order=2,
        )
        AttributeDefinition.objects.create(
            organization=self.org,
            key="internal_payload",
            name="Internal payload",
            field_type="text",
            display_order=3,
        )
        call = self.call()
        lead = call.lead
        lead.attributes = {
            "interest": "CRM automation",
            "budget": "₹50,000",
            "internal_payload": '{"trace_id":"hidden","score":0.91}',
            "unknown_system_key": "must not render",
        }
        lead.save(update_fields=["attributes"])

        response = self.api().get(f"/api/v1/call-intelligence/leads/{lead.id}/")
        self.assertEqual(response.status_code, 200)
        rows = response.data["lead"]["attribute_details"]
        self.assertEqual(
            rows,
            [
                {
                    "key": "interest",
                    "name": "Product interest",
                    "field_type": "text",
                    "value": "CRM automation",
                },
                {
                    "key": "budget",
                    "name": "Budget",
                    "field_type": "text",
                    "value": "₹50,000",
                },
            ],
        )

    def test_agent_cannot_choose_unassigned_pipeline(self):
        self.pipeline.owner = None
        self.pipeline.save(update_fields=["owner"])
        url = "/api/v1/call-intelligence/leads/"
        response = self.api().get(url)
        self.assertEqual(response.status_code, 200)
        allowed_ids = {row["id"] for row in response.data["pipelines"]}
        self.assertNotIn(str(self.pipeline.id), allowed_ids)
        result = self.api().post(url, {
            "name": "Restricted", "phone": "9876501236",
            "pipeline_id": str(self.pipeline.id), "stage_id": str(self.stage.id),
        }, format="json")
        self.assertEqual(result.status_code, 400)


    def test_mobile_today_dashboard_returns_actionable_counts(self):
        self.call(status="answered")
        missed = self.call(
            status="missed",
            talk_duration_seconds=0,
            ring_duration_seconds=12,
        )
        missed.disposition = ""
        missed.save(update_fields=["disposition"])
        LeadReminder.objects.create(
            lead=missed.lead,
            assigned_to=self.user,
            title="Call back",
            due_at=timezone.now() + timedelta(hours=1),
        )
        response = self.api().get("/api/v1/call-intelligence/today/")
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.data["stats"]["total"], 2)
        self.assertGreaterEqual(response.data["stats"]["missed"], 1)
        self.assertGreaterEqual(response.data["stats"]["followups_due"], 1)
        self.assertIn("recent_calls", response.data)
        self.assertIn("reminders", response.data)

    def test_mobile_today_mine_does_not_mix_admin_team_calls(self):
        own = self.call(status="answered")
        peer = User.objects.create_user(
            email="today-peer@example.com",
            organization=self.org,
            password="secret123",
            role=User.Role.AGENT,
        )
        other_call = CallRecord.objects.create(
            organization=self.org,
            user=peer,
            source="cloud",
            source_call_id="today-peer-call",
            phone_number="+919811112222",
            direction="incoming",
            status="missed",
            ended_at=timezone.now(),
        )
        response = self.api().get("/api/v1/call-intelligence/today/", {"mine": "1"})
        self.assertEqual(response.status_code, 200)
        ids = [row["id"] for row in response.data["recent_calls"]]
        self.assertIn(str(own.id), ids)
        self.assertNotIn(str(other_call.id), ids)

    def test_mobile_call_detail_includes_history_and_crm_context(self):
        first = self.call()
        second = self.call(phone_number=first.phone_number)
        response = self.api().get(
            f"/api/v1/call-intelligence/calls/{second.id}/"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["call"]["lead"]["id"], str(second.lead_id))
        self.assertTrue(any(row["id"] == str(first.id) for row in response.data["history"]))
        self.assertIn("dispositions", response.data)

    def test_mobile_lead_list_detail_move_and_follow_up_are_scoped(self):
        call = self.call()
        lead = call.lead
        next_stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="Mobile next",
            display_order=99,
        )
        listing = self.api().get("/api/v1/call-intelligence/leads/list/", {"q": lead.phone[-6:]})
        self.assertEqual(listing.status_code, 200)
        self.assertIn(str(lead.id), [row["id"] for row in listing.data["results"]])

        detail_url = f"/api/v1/call-intelligence/leads/{lead.id}/"
        detail = self.api().get(detail_url)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["lead"]["pipeline_id"], str(self.pipeline.id))
        moved = self.api().patch(
            detail_url,
            {"pipeline_id": str(self.pipeline.id), "stage_id": str(next_stage.id)},
            format="json",
        )
        self.assertEqual(moved.status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.stage_id, next_stage.id)

        due_at = (timezone.now() + timedelta(days=1)).isoformat()
        reminder = self.api().post(
            detail_url,
            {"due_at": due_at, "title": "Mobile follow-up"},
            format="json",
        )
        self.assertEqual(reminder.status_code, 201)
        self.assertTrue(
            LeadReminder.objects.filter(lead=lead, status="pending").exists()
        )
        self.assertEqual(self.api(self.other_user).get(detail_url).status_code, 404)

    def test_mobile_reminder_segments_include_completed_history(self):
        _, row = self.reminder()
        action_url = f"/api/v1/call-intelligence/reminders/{row.id}/action/"
        self.assertEqual(
            self.api().post(action_url, {"action": "complete"}, format="json").status_code,
            200,
        )
        completed = self.api().get(
            "/api/v1/call-intelligence/reminders/",
            {"status": "completed"},
        )
        self.assertEqual(completed.status_code, 200)
        self.assertIn(str(row.id), [item["id"] for item in completed.data["reminders"]])
        self.assertGreaterEqual(completed.data["stats"]["completed"], 1)

    def test_mobile_call_filters_support_follow_up_and_unlinked_segments(self):
        call = self.call()
        call.follow_up_required = True
        call.follow_up_at = timezone.now() + timedelta(days=1)
        call.save(update_fields=["follow_up_required", "follow_up_at"])
        response = self.api().get(
            "/api/v1/call-intelligence/calls/",
            {"mine": "1", "needs_follow_up": "1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(str(call.id), [row["id"] for row in response.data["calls"]])
