from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.crm.models import Lead, Pipeline
from apps.telephony.models import CallDisposition, CallIntelligenceSettings, CallRecord

from . import test_mobile_workspace as fixtures


class CallActivityWorkspaceTests(TestCase):
    setUp = fixtures.MobileWorkspaceTests.setUp
    payload = fixtures.MobileWorkspaceTests.payload
    call = fixtures.MobileWorkspaceTests.call
    web = fixtures.MobileWorkspaceTests.web
    api = fixtures.MobileWorkspaceTests.api

    def unknown_call(self, **kwargs):
        CallIntelligenceSettings.objects.filter(organization=self.org).update(enabled=False)
        return self.call(**kwargs)

    def create(self, call, **kwargs):
        data = {"name": "New prospect", "pipeline": str(self.pipeline.id), "stage": str(self.stage.id)}
        data.update(kwargs)
        return self.web().post(
            reverse("call-intelligence-call-lead", args=[call.id]), data,
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )

    def test_outcome_tabs_count_full_history_and_keep_other_filters(self):
        now = timezone.now()
        CallRecord.objects.bulk_create([
            CallRecord(organization=self.org, user=self.user, phone_number="+919876543210",
                       source="cloud", source_call_id=f"history-{i}", direction="outgoing",
                       status="answered", ended_at=now, disposition="interested" if i < 53 else "")
            for i in range(61)
        ])
        response = self.web().get(reverse("call-intelligence-dashboard"), {
            "section": "analytics", "disposition": "interested", "source": "cloud", "page": "2",
        })
        self.assertEqual(response.context["call_page"].paginator.count, 53)
        self.assertEqual(len(response.context["recent_calls"]), 3)
        groups = {group["code"]: group for group in response.context["outcome_groups"]}
        self.assertEqual(groups[""]["total"], 61)
        self.assertEqual(groups["interested"]["total"], 53)
        self.assertEqual(groups["unclassified"]["total"], 8)
        self.assertTrue(groups["interested"]["selected"])
        params = parse_qs(urlsplit(groups["unclassified"]["url"]).query)
        self.assertEqual(params["disposition"], ["unclassified"])
        self.assertEqual(params["source"], ["cloud"])
        self.assertNotIn("page", params)
        self.assertContains(response, 'aria-label="Call outcomes"')
        self.assertContains(response, 'aria-current="page"')

    def test_crm_segments_and_logging_user_are_visible_before_opening_call(self):
        linked = self.call()
        unknown = self.unknown_call(phone_number="9876543211")
        response = self.web().get(reverse("call-intelligence-dashboard"), {"crm": "unlinked"})
        self.assertEqual(response.context["active_section"], "analytics")
        self.assertEqual([call.id for call in response.context["recent_calls"]], [unknown.id])
        groups = {group["code"]: group["total"] for group in response.context["crm_groups"]}
        self.assertEqual(groups, {"": 2, "linked": 1, "unlinked": 1})
        self.assertContains(response, "Lead not created")
        self.assertContains(response, 'class="ci-call-agent"')
        self.assertContains(response, self.user.name)
        self.assertContains(response, "Create lead")
        created = self.web().get(reverse("call-intelligence-dashboard"), {"crm": "linked"})
        self.assertEqual(created.context["recent_calls"][0].id, linked.id)
        self.assertContains(created, self.pipeline.name)
        self.assertContains(created, self.stage.name)
        self.assertContains(created, "View lead in CRM")
        self.assertContains(created, f"lead={linked.lead_id}")

    def test_create_chooses_exact_pipeline_stage_and_keeps_original_call_actor(self):
        call = self.unknown_call()
        destination = Pipeline.objects.create(organization=self.org, name="Sales team", owner=self.user)
        stage = destination.stages.get(name="Qualified")
        with self.captureOnCommitCallbacks(execute=True):
            response = self.create(call, pipeline=str(destination.id), stage=str(stage.id), phone="+441111111111")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["created"])
        call.refresh_from_db()
        self.assertEqual(call.lead.name, "New prospect")
        self.assertEqual(call.lead.phone, "+919876543210")
        self.assertEqual(call.lead.pipeline_id, destination.id)
        self.assertEqual(call.lead.stage_id, stage.id)
        self.assertEqual(call.crm_call.user_id, self.user.id)
        self.assertEqual(call.crm_call.duration_seconds, 80)
        self.assertEqual(call.lead.calls.count(), 1)
        again = self.create(call)
        self.assertFalse(again.json()["created"])
        call.refresh_from_db()
        self.assertEqual(call.lead.pipeline_id, destination.id)
        self.assertEqual(call.lead.stage_id, stage.id)
        self.assertEqual(call.lead.calls.count(), 1)
        history = self.web().get(response.json()["redirect_url"])
        self.assertEqual(history.context["recent_calls"][0].lead_id, call.lead_id)
        self.assertEqual(history.context["contact_history_name"], "New prospect")

    def test_existing_lead_is_linked_without_renaming_or_moving_it(self):
        call = self.unknown_call()
        peer = Pipeline.objects.create(organization=self.org, name="Peer pipeline")
        stage = peer.stages.get(name="Qualified")
        lead = Lead.objects.create(organization=self.org, pipeline=peer, stage=stage,
                                   name="Existing prospect", phone=call.phone_number)
        response = self.create(call, name="Do not rename", email="new@example.com")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        lead.refresh_from_db()
        call.refresh_from_db()
        self.assertEqual(call.lead_id, lead.id)
        self.assertEqual(lead.name, "Existing prospect")
        self.assertEqual(lead.pipeline_id, peer.id)
        self.assertEqual(lead.stage_id, stage.id)
        self.assertEqual(lead.email, "")
        self.assertEqual(lead.calls.count(), 1)
        response = self.web().get(reverse("call-intelligence-dashboard"))
        self.assertEqual(response.context["recent_calls"][0].crm_url, "")

    def test_admin_can_create_in_peer_pipeline_without_reassigning_call_user(self):
        call = self.unknown_call(notes="Customer asked for a price estimate.")
        peer = User.objects.create_user(email="sales-peer@example.com", organization=self.org, password="secret123")
        destination = Pipeline.objects.create(organization=self.org, name="Team pipeline", owner=peer)
        stage = destination.stages.get(name="Qualified")
        CallRecord.objects.filter(pk=call.pk).update(user=peer)
        self.user.role = User.Role.ADMIN
        self.user.save(update_fields=["role"])
        with patch("apps.channels.welcome_tasks.send_lead_welcome_task.delay") as welcome:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.create(call, pipeline=str(destination.id), stage=str(stage.id))
        self.assertEqual(response.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.lead.pipeline_id, destination.id)
        self.assertEqual(call.user_id, peer.id)
        self.assertEqual(call.crm_call.user_id, peer.id)
        self.assertEqual(call.crm_call.notes, "Customer asked for a price estimate.")
        welcome.assert_not_called()

    def test_invalid_destinations_and_input_do_not_partially_create_or_link(self):
        call = self.unknown_call()
        peer = Pipeline.objects.create(organization=self.org, name="Peer pipeline")
        foreign = Pipeline.objects.get(organization=self.other_org, name="Leads")
        cases = [
            {"pipeline": str(peer.id), "stage": str(peer.stages.first().id)},
            {"pipeline": str(foreign.id), "stage": str(foreign.stages.first().id)},
            {"stage": str(peer.stages.first().id)},
            {"pipeline": "invalid"}, {"stage": ""}, {"name": ""},
            {"email": "not-an-email"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.create(call, **case).status_code, 400)
                call.refresh_from_db()
                self.assertIsNone(call.lead_id)
                self.assertIsNone(call.crm_call_id)
                self.assertEqual(Lead.objects.filter(organization=self.org).count(), 0)

    def test_creation_is_post_only_tenant_scoped_and_agent_scoped(self):
        call = self.unknown_call()
        url = reverse("call-intelligence-call-lead", args=[call.id])
        self.assertEqual(self.web().get(url).status_code, 405)
        self.assertEqual(self.web(self.other_user).post(url).status_code, 404)
        peer = User.objects.create_user(email="peer-calls@example.com", organization=self.org, password="secret123")
        self.assertEqual(self.web(peer).post(url).status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.cookies = self.web().cookies
        self.assertEqual(csrf_client.post(url).status_code, 403)

    def test_contact_history_uses_linked_lead_instead_of_phone_substrings(self):
        first = self.call()
        second = self.call()
        CallRecord.objects.filter(pk=second.pk).update(phone_number="+00919876543210")
        self.call(phone_number="9876543211")
        response = self.web().get(reverse("call-intelligence-dashboard"), {"lead": str(first.lead_id)})
        self.assertEqual(response.context["stats"]["total"], 2)
        self.assertEqual({call.id for call in response.context["recent_calls"]}, {first.id, second.id})
        empty = self.web().get(reverse("call-intelligence-dashboard"), {
            "lead": str(first.lead_id), "disposition": "converted",
        })
        self.assertEqual(empty.context["contact_history_name"], first.lead.name)
        self.assertEqual(empty.context["call_page"].paginator.count, 0)
        self.assertEqual(self.web().get(reverse("call-intelligence-dashboard"), {"lead": "invalid"}).context["stats"]["total"], 0)

    def test_changing_outcome_returns_updated_group_navigation_and_preserves_filters(self):
        call = self.call()
        response = self.web().post(
            reverse("call-intelligence-call-action", args=[call.id]) + "?source=android_sim&crm=linked&page=2",
            {"notes": "Interested in a demo", "disposition": "interested"},
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertTrue(response.json()["outcome_changed"])
        params = parse_qs(urlsplit(response.json()["activity_url"]).query)
        self.assertEqual(params["disposition"], ["interested"])
        self.assertEqual(params["source"], ["android_sim"])
        self.assertEqual(params["crm"], ["linked"])
        self.assertNotIn("page", params)
        self.assertEqual(params["open"], [str(call.id)])

    def test_historical_disabled_outcome_keeps_its_label_and_is_navigable(self):
        call = self.call()
        CallDisposition.objects.create(organization=self.org, code="old_review", name="Needs review",
                                       is_active=False, category="connected")
        CallRecord.objects.filter(pk=call.pk).update(disposition="old_review")
        response = self.web().get(reverse("call-intelligence-dashboard"), {"disposition": "old_review"})
        group = next(item for item in response.context["outcome_groups"] if item["code"] == "old_review")
        self.assertEqual(group["total"], 1)
        self.assertContains(response, "Needs review")
        self.assertContains(response, 'value="old_review" selected')
        saved = self.web().post(reverse("call-intelligence-call-action", args=[call.id]), {
            "notes": "Keep the original classification", "disposition": "old_review",
        }, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(saved.status_code, 200)
        call.refresh_from_db()
        self.assertEqual(call.disposition, "old_review")
