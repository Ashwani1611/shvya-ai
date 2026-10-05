"""The downloadable Brain and bounded runtime share a fresh canonical snapshot."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.ai_engagement.models import Document, FAQ, OrgInfo
from apps.ai_engagement.services.context import AIContextBuilder, AIContextError
from apps.ai_engagement.services.organization_brain_bundle import (
    _assemble_bundle,
    get_organization_ai_brain_bundle,
)
from apps.ai_engagement.services.organization_profile import compile_org_ai_profile_from_context
from apps.ai_engagement.services.org_info import OrgInfoService
from apps.ai_engagement.services.organization_runtime_profile import (
    MAX_KNOWLEDGE_SOURCES,
    OrganizationAIRuntimeProfileBuilder,
    get_organization_ai_runtime_profile,
)
from apps.ai_engagement.services.playground import _SandboxContextBuilder, _SandboxLead
from apps.ai_engagement.services.tenant_guard import TenantScopeError
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.organizations.models import Organization
from apps.ai_engagement.tests.test_organization_brain_bundle import _rows as _bundle_rows
from tests.playbook_fixtures import build_ai_playbook


class BrainBundleProjectionTests(SimpleTestCase):
    def setUp(self):
        self.rows = _bundle_rows()
        self.rows["pipelines"][0]["is_active"] = True
        self.rows["stages"][0]["is_active"] = True
        self.rows["documents"][0]["is_active"] = True
        self.rows["faqs"][0]["answer"] = "FULL_FAQ_ANSWER_NOT_INCLUDED_IN_RUNTIME"
        self.rows["ai"]["ai_playbook"] = build_ai_playbook(questions=(
            "[id:goal] What is your goal?\nA) Sales\nB) Support"
        ))

    def build_profile(self, bundle, *, settings=None):
        organization = SimpleNamespace(id=bundle["organization"]["id"], settings=settings or {})
        with patch("apps.ai_engagement.services.organization_brain_bundle.get_organization_ai_brain_bundle",
                   return_value=bundle) as canonical_loader, \
             patch("apps.channels.models.WhatsAppAccount.objects.filter") as account_query:
            account_query.return_value.order_by.return_value.values.return_value = []
            profile = OrganizationAIRuntimeProfileBuilder().build(organization=organization)
        canonical_loader.assert_called_once_with(organization=organization)
        account_query.assert_called_once_with(organization_id=organization.id)
        return profile

    def test_runtime_uses_canonical_safe_projection_and_keeps_ready_file_before_indexing(self):
        # Validated guided uploads can be shareable before indexed publication.
        self.rows["documents"][0]["is_active"] = False
        bundle = _assemble_bundle(**self.rows)
        profile = self.build_profile(bundle)
        self.assertEqual(profile.brain_bundle_revision, bundle["revision"])
        self.assertEqual(profile.as_dict()["brain_bundle"], {
            "schema_version": bundle["schema_version"], "revision": bundle["revision"],
        })
        self.assertEqual(profile.legacy_organization_context()["ai_playbook"], bundle["ai"]["ai_playbook"])
        self.assertEqual([item["stable_id"] for item in profile.configured_requirements()], ["goal"])
        document, = profile.as_dict()["knowledge_sources"]
        self.assertEqual(document["source_key"], "https://example.com/pricing")
        self.assertEqual(document["source_url"], "https://example.com/pricing")
        self.assertEqual(document["processing_status"], "pending")
        self.assertTrue(document["file_sharing_ready"])
        self.assertTrue(document["has_file"])
        serialized = json.dumps(profile.as_dict())
        for private in ("user:password", "token=secret", "#private", "account-secret", "stage-secret",
                        "nested-secret", "FULL_FAQ_ANSWER_NOT_INCLUDED_IN_RUNTIME"):
            self.assertNotIn(private, serialized)

    def test_faq_change_outside_prompt_projection_changes_runtime_revision(self):
        first = self.build_profile(_assemble_bundle(**self.rows))
        self.rows["faqs"][0]["answer"] = "CHANGED_FULL_FAQ_ANSWER"
        second = self.build_profile(_assemble_bundle(**self.rows))
        self.assertNotEqual(first.brain_bundle_revision, second.brain_bundle_revision)
        self.assertNotEqual(first.revision, second.revision)
        self.assertEqual(first.legacy_organization_context(), second.legacy_organization_context())
        self.assertEqual(first.as_dict()["knowledge_sources"], second.as_dict()["knowledge_sources"])


class BrainBundleRuntimeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Bundle runtime company", timezone="Asia/Kolkata")
        self.foreign = Organization.objects.create(name="Foreign bundle company")
        self.playbook = build_ai_playbook(questions=(
            "[id:need] What help do you need?\nA) Follow-ups\nB) Tracking\n"
            "[id:tool] Where do you manage leads?\nA) Sheets\nB) CRM"
        ), rules="Reply naturally in the configured language.")
        OrgInfo.objects.update_or_create(organization=self.org, defaults={
            "about": "OUR_COMPANY_DESCRIPTION", "bot_languages": "English, Hinglish",
            "ai_playbook": self.playbook, "qualification_model": "configured-qualification-model",
            "sales_support_model": "configured-sales-model", "summary_model": "configured-summary-model",
        })
        OrgInfo.objects.update_or_create(organization=self.foreign, defaults={
            "about": "FOREIGN_COMPANY_DESCRIPTION", "ai_playbook": "FOREIGN_PLAYBOOK",
        })
        self.faq = FAQ.objects.create(organization=self.org, question="What do you offer?", answer="OUR_FULL_FAQ_ANSWER")
        FAQ.objects.create(organization=self.foreign, question="What do you offer?", answer="FOREIGN_FAQ_ANSWER")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales")
        self.stage = self.pipeline.stages.get(name="New leads")
        self.lead = Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage,
                                       name="Runtime test lead", phone="+919900000001")
        AttributeDefinition.objects.create(organization=self.org, key="need", name="Need")
        AttributeDefinition.objects.create(organization=self.foreign, key="need", name="FOREIGN_ATTRIBUTE")

    def assert_bundle_link(self, profile, bundle):
        self.assertEqual(profile.brain_bundle_revision, bundle["revision"])
        self.assertEqual(profile.brain_bundle_schema_version, bundle["schema_version"])
        self.assertEqual(profile.as_dict()["brain_bundle"], {
            "schema_version": bundle["schema_version"], "revision": bundle["revision"],
        })
        self.assertEqual(profile.profile_version, "phase4.v2")

    def test_bounded_runtime_links_complete_bundle_and_preserves_authored_qualification(self):
        bundle = get_organization_ai_brain_bundle(organization=self.org)
        profile = OrganizationAIRuntimeProfileBuilder().build(organization=self.org, lead=self.lead)
        self.assert_bundle_link(profile, bundle)
        legacy = profile.legacy_organization_context()
        self.assertEqual(legacy["about"], bundle["ai"]["about"])
        self.assertEqual(legacy["ai_playbook"], self.playbook)
        for field in ("bot_languages", "qualification_model", "sales_support_model", "summary_model"):
            self.assertEqual(legacy[field], bundle["ai"][field])
        expected = compile_org_ai_profile_from_context(legacy)["qualification"]["requirements"]
        self.assertEqual(profile.configured_requirements(), expected)
        self.assertEqual([item["stable_id"] for item in expected], ["need", "tool"])
        self.assertNotIn("OUR_FULL_FAQ_ANSWER", json.dumps(profile.as_dict()))
        self.assertNotIn("FOREIGN_", json.dumps(profile.as_dict()))
        self.assertNotIn("FOREIGN_", json.dumps(bundle))

    def test_new_context_on_same_lead_refreshes_after_faq_bulk_update_without_timestamp_change(self):
        builder = AIContextBuilder()
        builder.build(organization=self.org, lead=self.lead)
        first = get_organization_ai_runtime_profile(organization=self.org, lead=self.lead)
        original_timestamp = self.faq.updated_at
        FAQ.objects.filter(pk=self.faq.pk, organization=self.org).update(answer="UPDATED_FAQ_WITHOUT_TIMESTAMP")
        self.faq.refresh_from_db()
        self.assertEqual(self.faq.updated_at, original_timestamp)
        builder.build(organization=self.org, lead=self.lead)
        refreshed = get_organization_ai_runtime_profile(organization=self.org, lead=self.lead)
        self.assertNotEqual(first.brain_bundle_revision, refreshed.brain_bundle_revision)
        self.assertNotEqual(first.revision, refreshed.revision)
        self.assert_bundle_link(refreshed, get_organization_ai_brain_bundle(organization=self.org))

    def test_new_context_rejects_a_foreign_lead_before_loading_its_profile(self):
        get_organization_ai_runtime_profile(organization=self.org, lead=self.lead)
        with self.assertRaises(TenantScopeError):
            get_organization_ai_runtime_profile(organization=self.foreign, lead=self.lead)
        with self.assertRaisesMessage(AIContextError, "Lead does not belong"):
            AIContextBuilder().build(organization=self.foreign, lead=self.lead)

    def test_long_tail_playbook_edit_changes_bundle_and_policy_revisions(self):
        info = OrgInfo.objects.get(organization=self.org)
        first_text = self.playbook + "\n" + "x" * 13000 + "\nTAIL_POLICY_ONE"
        OrgInfo.objects.filter(pk=info.pk).update(ai_playbook=first_text)
        first = OrganizationAIRuntimeProfileBuilder().build(organization=self.org)
        OrgInfo.objects.filter(pk=info.pk).update(ai_playbook=first_text.replace("TAIL_POLICY_ONE", "TAIL_POLICY_TWO"))
        second = OrganizationAIRuntimeProfileBuilder().build(organization=self.org)
        self.assertNotEqual(first.brain_bundle_revision, second.brain_bundle_revision)
        self.assertNotEqual(first.revision, second.revision)
        self.assertIn("TAIL_POLICY_TWO", second.legacy_organization_context()["ai_playbook"])

    def test_policy_permissions_change_composite_revision_without_changing_download_revision(self):
        first = OrganizationAIRuntimeProfileBuilder().build(organization=self.org)
        self.org.settings = {"ai_action_permissions": {"allowed_action_types": []}}
        self.org.save(update_fields=["settings"])
        second = OrganizationAIRuntimeProfileBuilder().build(organization=self.org)
        self.assertEqual(first.brain_bundle_revision, second.brain_bundle_revision)
        self.assertNotEqual(first.revision, second.revision)
        self.assertEqual(second.as_dict()["crm_capabilities"]["allowed_action_types"], [])

    def test_runtime_document_projection_is_bounded_without_loading_knowledge_chunks(self):
        Document.objects.bulk_create([
            Document(organization=self.org, name=f"Guide {index:03d}", source_key=f"guide-{index:03d}",
                     processing_status=Document.ProcessingStatus.COMPLETED)
            for index in range(MAX_KNOWLEDGE_SOURCES + 5)
        ])
        bundle = get_organization_ai_brain_bundle(organization=self.org)
        self.assertEqual(len(bundle["knowledge"]["documents"]), MAX_KNOWLEDGE_SOURCES + 5)
        with CaptureQueriesContext(connection) as queries:
            profile = OrganizationAIRuntimeProfileBuilder().build(organization=self.org)
        self.assertEqual(len(profile.as_dict()["knowledge_sources"]), MAX_KNOWLEDGE_SOURCES)
        self.assert_bundle_link(profile, bundle)
        self.assertFalse(any("ai_engagement_chunk" in item["sql"].lower() for item in queries))
        self.assertLessEqual(len(queries), 12)

    def test_sandbox_organization_context_uses_and_refreshes_same_bundle(self):
        visitor = _SandboxLead(id="bundle-preview", pk="bundle-preview", organization_id=self.org.pk,
            pipeline=self.pipeline, pipeline_id=self.pipeline.pk, stage=self.stage, stage_id=self.stage.pk, attributes={})
        def new_turn_builder():
            return _SandboxContextBuilder(organization=self.org, visitor=visitor, conversation=[],
                org_info_service=OrgInfoService(), embedding_service=SimpleNamespace(), retrieval_service=SimpleNamespace(),
                pipeline=self.pipeline, stage=self.stage)
        builder = new_turn_builder()
        first_context = builder.organization_context()
        first = get_organization_ai_runtime_profile(organization=self.org, lead=visitor)
        self.assert_bundle_link(first, get_organization_ai_brain_bundle(organization=self.org))
        self.assertEqual(first_context["ai_playbook"], self.playbook)
        FAQ.objects.filter(pk=self.faq.pk, organization=self.org).update(answer="SANDBOX_UPDATED_FAQ")
        builder.organization_context()
        self.assertIs(first, get_organization_ai_runtime_profile(organization=self.org, lead=visitor))
        self.assertEqual(builder.brain_bundle_metadata()["revision"], first.brain_bundle_revision)
        next_builder = new_turn_builder()
        next_builder.organization_context()
        refreshed = get_organization_ai_runtime_profile(organization=self.org, lead=visitor)
        self.assertNotEqual(first.brain_bundle_revision, refreshed.brain_bundle_revision)
        self.assert_bundle_link(refreshed, get_organization_ai_brain_bundle(organization=self.org))
        self.assertEqual(next_builder.brain_bundle_metadata()["revision"], refreshed.brain_bundle_revision)
