from unittest.mock import patch
from tempfile import TemporaryDirectory

from django.core.files.uploadedfile import SimpleUploadedFile

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.models import AICreditWallet, Document, FAQ, OrgInfo
from apps.crm.models import Pipeline, Stage
from apps.organizations.models import Organization
from apps.superadmin.ai_brain_services import brain_summary, revision_token


class AIBrainConsoleTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Brain tenant")
        self.other = Organization.objects.create(name="Other tenant")
        self.info = OrgInfo.objects.create(organization=self.org, about="Original")
        self.other_info = OrgInfo.objects.create(organization=self.other, about="Private business")
        self.url = reverse("superadmin-organization-ai-brain", args=[self.org.pk])
        self.admin = User.objects.create_superuser(email="brain-admin@example.com", password="test", name="Admin")
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

    def brain_data(self):
        return dict(action="brain", revision=revision_token(self.org, self.info), about="Updated business",
                    bot_languages="English, Hindi", ai_playbook="", ai_enabled="on", bump_up_count=3)

    def test_get_is_scoped_and_has_working_sections(self):
        FAQ.objects.create(organization=self.other, question="Other secret", answer="Private answer")
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        for label in ("AI Brain &amp; Playbook", "Knowledge sources", "FAQs", "Pipeline &amp; stage AI", "Advanced model routing"):
            self.assertContains(response, label)
        self.assertNotContains(response, "Other secret")
        self.assertNotContains(response, "Private business")

    def test_brain_save_is_tenant_scoped_and_preserves_model_routing(self):
        self.info.qualification_model = "existing-model"
        self.info.save()
        response = self.client.post(self.url, self.brain_data())
        self.assertEqual(response.status_code, 302)
        self.info.refresh_from_db()
        self.other_info.refresh_from_db()
        self.assertEqual(self.info.about, "Updated business")
        self.assertEqual(self.info.qualification_model, "existing-model")
        self.assertFalse(self.info.bump_up_enabled)
        self.assertEqual(self.other_info.about, "Private business")

    def test_concurrent_edit_is_not_overwritten(self):
        data = self.brain_data()
        self.info.about = "Newer client edit"
        self.info.save()
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 409)
        self.info.refresh_from_db()
        self.assertEqual(self.info.about, "Newer client edit")
        self.assertContains(response, "Updated business", status_code=409)

    def test_faq_create_edit_and_cross_tenant_rejection(self):
        response = self.client.post(self.url, dict(action="faq", question="Hours?", answer="Nine to five", is_active="on"))
        self.assertEqual(response.status_code, 302)
        faq = FAQ.objects.get(organization=self.org)
        response = self.client.post(self.url, dict(action="faq", faq_id=faq.pk, revision=revision_token(self.org, faq, "faq"), question="Hours?", answer="Ten to six"))
        self.assertEqual(response.status_code, 302)
        faq.refresh_from_db()
        self.assertEqual(faq.answer, "Ten to six")
        self.assertFalse(faq.is_active)
        other = FAQ.objects.create(organization=self.other, question="Private?", answer="Secret")
        response = self.client.post(self.url, dict(action="faq", faq_id=other.pk, revision=revision_token(self.other, other, "faq"), question="Overwrite?", answer="No"))
        self.assertEqual(response.status_code, 404)
        other.refresh_from_db()
        self.assertEqual(other.answer, "Secret")

    def test_pipeline_stage_controls_are_scoped(self):
        for org in (self.org, self.other):
            pipeline = Pipeline.objects.create(organization=org, name="Sales")
            stage = Stage.objects.create(pipeline=pipeline, name="New")
            for action, target in (("pipeline_ai", pipeline), ("stage_ai", stage)):
                response = self.client.post(self.url, dict(action=action, target_id=target.pk, revision=revision_token(org, target, action)))
                self.assertEqual(response.status_code, 302 if org == self.org else 404, response.context and response.context.get("error"))
                target.refresh_from_db()
                if org == self.org:
                    self.assertFalse(target.ai_enabled if action == "pipeline_ai" else target.ai_on)

    @patch("apps.ai_engagement.services.knowledge_url_security.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
    def test_url_is_queued_after_commit_for_selected_tenant(self, dns):
        with patch("apps.superadmin.ai_brain_services.ingest_and_index_url_source.delay") as queue:
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(self.url, dict(action="url", name="Company website", url="https://example.com/about"))
            self.assertEqual(response.status_code, 302, response.context and response.context.get("error"))
            queue.assert_called_once()
            self.assertEqual(queue.call_args.kwargs["organization_id"], self.org.pk)

    def test_invalid_upload_and_missing_revision_do_not_save(self):
        self.assertEqual(self.client.post(self.url, {"action": "upload"}).status_code, 400)
        data = self.brain_data()
        data["revision"] = "invalid"
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.info.refresh_from_db()
        self.assertEqual(self.info.about, "Original")

    def test_anonymous_access_is_rejected(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.post(self.url, self.brain_data()).status_code, 302)

    def test_file_upload_and_instruction_use_canonical_validation(self):
        with TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            with patch("apps.superadmin.ai_brain_services.ingest_and_index_document.delay") as queue:
                with self.captureOnCommitCallbacks(execute=True):
                    response = self.client.post(self.url, dict(action="upload", name="Pricing", file=SimpleUploadedFile("pricing.txt", b"Plan costs 100 per month", content_type="text/plain"), share_instruction="Send when asked about pricing"))
                self.assertEqual(response.status_code, 302, response.context and response.context.get("error"))
                queue.assert_called_once()
                self.assertEqual(queue.call_args.kwargs["organization_id"], self.org.pk)
            document = Document.objects.get(organization=self.org)
            self.assertEqual(document.share_instruction, "Send when asked about pricing")
            response = self.client.post(self.url, dict(action="file_instruction", document_id=document.pk, revision=revision_token(self.org, document, "document"), share_instruction="Send the pricing guide on request"))
            self.assertEqual(response.status_code, 302)
            document.refresh_from_db()
            self.assertTrue(document.file_sharing_ready)
            self.assertEqual(document.share_instruction, "Send the pricing guide on request")

    def test_wallet_summary_uses_ai_wallet_and_does_not_create_on_read(self):
        self.assertIsNone(brain_summary(self.org)["wallet"])
        self.assertFalse(AICreditWallet.objects.filter(organization=self.org).exists())
        AICreditWallet.objects.create(organization=self.org, balance=300, reserved_credits=60, lifetime_credits_used=90)
        AICreditWallet.objects.create(organization=self.other, balance=9000)
        summary = brain_summary(self.org)
        self.assertEqual(summary["coins_available"], 8)
        self.assertEqual(summary["coins_used"], 3)
        self.assertEqual(summary["coins_reserved"], 2)
