from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.instagram_models import InstagramAccount, InstagramConversation, InstagramMessage
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, LeadCall, LeadNote
from apps.crm.templatetags.crm_extras import lead_avatar, lead_creation_source, lead_note_count
from apps.organizations.models import Organization
from apps.ai_engagement.services.intent_score import compute_intent_score, prepare_intent_scores, persist_intent_score
from services.channels.instagram_leads import link_instagram_lead
from services.crm.lead_chat import lead_chat_url
from services.channels.whatsapp_api_chat_service import get_api_conversation_messages, mark_api_conversation_read


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class LeadCardUpgradeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Card upgrade")
        self.pipeline = self.org.pipelines.first()
        self.pipeline.phone_number = "+919000000001"
        self.pipeline.save()
        self.lead = Lead.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=self.pipeline.stages.first(), name="A customer", phone="+919000000099")
        self.user = User.objects.create_user(email="card@example.com", password="password",
            name="Admin", role=User.Role.ADMIN, organization=self.org)
        session = self.client.session
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key
        self.account = WhatsAppAccount.objects.create(organization=self.org, status="connected",
            connection_type="api", display_phone_number="+91 90000 00001", phone_number_id="123456")

    def message(self, body, account=None, lead=None, direction="inbound"):
        return WhatsAppMessage.objects.create(organization=self.org, account=account or self.account,
            lead=lead or self.lead, direction=direction, status="received", body=body,
            from_number=self.lead.phone, to_number=self.account.display_phone_number)

    def instagram(self, linked=True):
        account = InstagramAccount.objects.create(organization=self.org, ig_user_id="ig-account")
        return InstagramConversation.objects.create(organization=self.org, account=account,
            participant_id="customer", lead=self.lead if linked else None)

    def test_no_evidence_is_unassessed_but_greeting_can_score_zero(self):
        self.assertIsNone(compute_intent_score(lead=self.lead)["score"])
        self.message("hello")
        prepare_intent_scores([self.lead])
        self.assertEqual(self.lead.intent_score_state["score"], 0)
        self.assertTrue(self.lead.intent_score_state["assessed"])

    def test_outbound_and_blank_messages_are_not_scoring_evidence(self):
        self.message("Book a demo tomorrow", direction="outbound")
        self.message("   ")
        self.assertIsNone(compute_intent_score(lead=self.lead)["score"])

    def test_instagram_and_whatsapp_share_rubric(self):
        text = "We need CRM for sales. Please book a demo tomorrow."
        self.message(text)
        expected = compute_intent_score(lead=self.lead)["score"]
        WhatsAppMessage.objects.all().delete()
        conversation = self.instagram()
        InstagramMessage.objects.create(organization=self.org, account=conversation.account,
            conversation=conversation, direction="inbound", status="received", body=text)
        prepare_intent_scores([self.lead])
        self.assertEqual(self.lead.intent_score_state["score"], expected)

    def test_unlinked_instagram_does_not_guess_identity(self):
        conversation = self.instagram(linked=False)
        InstagramMessage.objects.create(organization=self.org, account=conversation.account,
            conversation=conversation, direction="inbound", status="received", body="Book a demo tomorrow")
        self.assertIsNone(compute_intent_score(lead=self.lead)["score"])

    def test_batched_scores_have_constant_queries_and_bounded_evidence(self):
        for _ in range(45):
            self.message("hello")
        other = Lead.objects.create(organization=self.org, pipeline=self.pipeline,
            stage=self.lead.stage, name="Other", phone="+919000000098")
        with self.assertNumQueries(2):
            prepare_intent_scores([self.lead, other])
        self.assertEqual(self.lead.intent_score_state["evidence_count"], 40)
        self.assertIsNone(other.intent_score_state["score"])

    def test_persist_does_not_overwrite_concurrent_attributes(self):
        Lead.objects.filter(pk=self.lead.pk).update(attributes={"important": "new value"})
        persist_intent_score(lead=self.lead)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.attributes["important"], "new value")

    def test_notes_are_lead_scoped_and_legacy_mirror_is_not_double_counted(self):
        LeadNote.objects.create(lead=self.lead, note="First")
        LeadNote.objects.create(lead=self.lead, note="Second")
        self.lead.notes = "Second"
        self.assertEqual(lead_note_count(self.lead), 2)
        self.lead.notes = "Legacy only"
        self.assertEqual(lead_note_count(self.lead), 3)

    def test_all_call_attempt_statuses_are_counted(self):
        from apps.crm.views.dashboard import _lead_card_context
        for status, _ in LeadCall._meta.get_field("status").choices:
            LeadCall.objects.create(lead=self.lead, status=status, called_at=timezone.now())
        context = _lead_card_context(self.lead, self.user)
        self.assertEqual(context["lead"].call_count, 5)

    def test_avatar_is_stable_varied_and_does_not_include_customer_text(self):
        avatar = lead_avatar(self.lead)
        self.lead.name = "<script>alert(1)</script>"
        self.assertEqual(lead_avatar(self.lead), avatar)
        self.assertNotIn("script", avatar)
        self.assertNotEqual(avatar, lead_avatar(SimpleNamespace(pk="another-lead")))

    def test_activity_uses_recorded_creation_source(self):
        for source, label in {"system":"System", "whatsapp_api":"WhatsApp API", "whatsapp":"WhatsApp",
                "instagram":"Instagram", "csv_import":"CSV Import", "meta_ads":"Meta Ads",
                "google_sheets":"Google Sheet", "external_api":"External API"}.items():
            self.assertEqual(lead_creation_source(SimpleNamespace(details={"lead_source":source}), self.lead), label)

    def test_routing_prefers_current_pipeline_over_old_conversation(self):
        other = WhatsAppAccount.objects.create(organization=self.org, status="connected",
            display_phone_number="+919000000002", phone_number_id="654321")
        self.message("old conversation", account=other)
        url = lead_chat_url(self.lead)
        self.assertEqual(parse_qs(urlparse(url).query)["account"], [str(self.account.pk)])
        response = self.client.get(reverse("crm-lead-whatsapp", args=[self.lead.pk]))
        self.assertEqual(response.url, url)

    def test_hosted_routing_and_missing_or_ambiguous_mapping(self):
        self.account.connection_type = "hosted"
        self.account.save()
        self.assertIn("connect/hosted/", lead_chat_url(self.lead))
        self.assertEqual(parse_qs(urlparse(lead_chat_url(self.lead)).query)["chat"], [self.lead.phone])
        WhatsAppAccount.objects.create(organization=self.org, status="connected",
            display_phone_number=self.pipeline.phone_number)
        with self.assertRaises(ValidationError):
            lead_chat_url(self.lead)
        self.pipeline.phone_number = "+919111111111"
        with self.assertRaises(ValidationError):
            lead_chat_url(self.lead)

    def test_account_scope_preserves_other_number_history_and_unread(self):
        other = WhatsAppAccount.objects.create(organization=self.org, status="connected",
            display_phone_number="+919000000002", phone_number_id="654321")
        first = self.message("selected")
        second = self.message("other", account=other)
        WhatsAppMessage.objects.update(is_read=False)
        rows = get_api_conversation_messages(organization=self.org, lead=self.lead, account=self.account)
        self.assertEqual(list(rows), [first])
        mark_api_conversation_read(organization=self.org, lead=self.lead, account=self.account)
        second.refresh_from_db()
        self.assertFalse(second.is_read)

    def test_link_preserves_existing_source_and_rejects_reassignment(self):
        conversation = self.instagram(linked=False)
        link_instagram_lead(user=self.user, conversation_id=conversation.pk, phone=self.lead.phone)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.lead_source, "system")
        with self.assertRaises(ValidationError):
            link_instagram_lead(user=self.user, conversation_id=conversation.pk, phone="+919888888888")

    def test_link_new_lead_has_instagram_source_and_requires_real_phone(self):
        conversation = self.instagram(linked=False)
        with self.assertRaises(ValidationError):
            link_instagram_lead(user=self.user, conversation_id=conversation.pk, phone="instagram-user")
        linked = link_instagram_lead(user=self.user, conversation_id=conversation.pk,
            phone="+919888888888", name="Confirmed customer", pipeline_id=str(self.pipeline.pk))
        self.assertEqual(linked.lead.lead_source, "instagram")
        self.assertEqual(linked.lead.activities.get(topic="lead_created").details["lead_source"], "instagram")

    def test_cross_tenant_instagram_link_is_rejected(self):
        conversation = self.instagram(linked=False)
        other = Organization.objects.create(name="Other tenant")
        conversation.organization = other
        conversation.save()
        with self.assertRaises(InstagramConversation.DoesNotExist):
            link_instagram_lead(user=self.user, conversation_id=conversation.pk, phone=self.lead.phone)

    @patch("apps.channels.tasks.send_whatsapp_message_task.delay")
    def test_api_reply_uses_explicit_selected_account(self, send):
        other = WhatsAppAccount.objects.create(organization=self.org, status="connected",
            display_phone_number="+919000000002", phone_number_id="654321")
        self.message("hello")
        self.message("newest but wrong number", account=other)
        response = self.client.post(reverse("whatsapp-send-message", args=[self.lead.pk]) +
            "?account=" + str(self.account.pk), {"body":"A human reply"})
        self.assertEqual(response.status_code, 202)
        sent = WhatsAppMessage.objects.get(pk=response.json()["id"])
        self.assertEqual(sent.account_id, self.account.pk)
