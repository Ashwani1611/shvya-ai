"""The organization Brain bundle is complete configuration, never customer data."""
import json
from copy import deepcopy
from datetime import datetime, timezone as datetime_timezone
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.db import connection
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext

from apps.ai_engagement.models import Document, FAQ, KnowledgeSource, OrgInfo
from apps.ai_engagement.services.organization_brain_bundle import (
    _assemble_bundle, _safe_source_url, get_organization_ai_brain_bundle,
)
from apps.ai_engagement.services.tenant_guard import TenantScopeError
from apps.crm.models import AttributeDefinition, Lead, Pipeline
from apps.organizations.models import Organization


_NOW = datetime(2026, 10, 5, 10, 0, tzinfo=datetime_timezone.utc)


def _rows():
    attribute_id, foreign_id, pipeline_id = str(uuid4()), str(uuid4()), str(uuid4())
    return {
        "organization": {"id": str(uuid4()), "name": "Company", "timezone": "Asia/Kolkata"},
        "ai": {"about": "We automate sales.", "bot_languages": "English, Hinglish",
               "ai_playbook": "## Qualification Questions\nWhat is your goal?\n\n## Qualification Criteria\nAll questions answered.",
               "qualification_model": "qualification-model", "sales_support_model": "support-model",
               "summary_model": "summary-model", "ai_enabled": True, "bump_up_enabled": True,
               "bump_up_count": 2, "created_at": _NOW, "updated_at": _NOW},
        "faqs": [{"id": 1, "question": "Price?", "answer": "2999 per month.", "is_active": False,
                  "created_at": _NOW, "updated_at": _NOW}],
        "attributes": [{"id": attribute_id, "name": "Goal", "key": "goal", "field_type": "option",
                        "description": "Customer goal", "options": ["Sales", "Support"], "display_order": 0,
                        "is_active": True, "created_at": _NOW, "updated_at": _NOW}],
        "pipelines": [{"id": pipeline_id, "name": "Sales", "description": "Our sales process",
                       "country_code": "+91", "phone_number": "9000000000", "is_active": False,
                       "ai_enabled": True, "created_at": _NOW, "updated_at": _NOW}],
        "stages": [{"id": str(uuid4()), "pipeline_id": pipeline_id, "name": "Review", "description": "Review criteria",
                    "display_order": 3, "color": "blue", "is_active": False, "ai_on": True,
                    "config": {"required_attribute_ids": [attribute_id, foreign_id],
                               "api_key": "stage-secret", "nested": {"password": "nested-secret"}},
                    "created_at": _NOW, "updated_at": _NOW}],
        "sources": [{"id": 1, "source_type": "url", "name": "Pricing", "url": "https://user:password@example.com/pricing?token=secret#private",
                     "is_active": False, "created_at": _NOW, "updated_at": _NOW}],
        "documents": [{"id": 1, "name": "Product brochure", "version": 2,
                       "source_url": "https://user:password@example.com/pricing?token=secret#private",
                       "file": "private/storage/account-secret/brochure.pdf", "processing_status": "pending",
                       "is_active": False, "file_sharing_ready": True,
                       "share_instruction": "Share when the customer requests the brochure.",
                       "created_at": _NOW, "updated_at": _NOW}],
    }


class BrainBundleSerializationTests(SimpleTestCase):
    def setUp(self):
        self.rows = _rows()

    def test_generated_timestamp_is_outside_stable_full_content_revision(self):
        later = datetime(2026, 10, 5, 11, 0, tzinfo=datetime_timezone.utc)
        with patch("apps.ai_engagement.services.organization_brain_bundle.timezone.now", side_effect=[_NOW, later]):
            first, second = _assemble_bundle(**self.rows), _assemble_bundle(**self.rows)
        self.assertEqual(first["revision"], second["revision"])
        self.assertEqual(len(first["revision"]), 64)
        self.assertNotEqual(first["generated_at"], second["generated_at"])
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(json.loads(json.dumps(first, ensure_ascii=False)), first)

    def test_author_text_and_all_faq_records_are_preserved_without_runtime_caps(self):
        about = "a" * 30000 + " Company-specific tail."
        playbook = "## Rules\n" + "rule " * 12000 + "Important final exception."
        self.rows["ai"].update(about=about, ai_playbook=playbook)
        self.rows["faqs"] = [{**self.rows["faqs"][0], "id": index, "answer": "answer " * 2000 + str(index)}
                             for index in range(150)]
        bundle = _assemble_bundle(**self.rows)
        self.assertEqual(bundle["ai"]["about"], about)
        self.assertEqual(bundle["ai"]["ai_playbook"], playbook)
        self.assertEqual(len(bundle["faqs"]), 150)
        self.assertEqual(bundle["faqs"][-1]["answer"], self.rows["faqs"][-1]["answer"])
        self.rows["ai"]["about"] += " Changed beyond the old 12k revision limit."
        self.assertNotEqual(bundle["revision"], _assemble_bundle(**self.rows)["revision"])

    def test_every_authored_section_and_readiness_change_affects_revision(self):
        original = _assemble_bundle(**self.rows)["revision"]
        cases = (("ai", "sales_support_model", "changed-model"), ("ai", "bot_languages", "Marathi"),
                 ("ai", "ai_playbook", "## Rules\nA new business exception."),
                 ("faqs", "answer", "Changed price."), ("documents", "file_sharing_ready", False),
                 ("documents", "share_instruction", "Only when qualified."), ("documents", "is_active", True),
                 ("documents", "version", 3), ("sources", "is_active", True),
                 ("attributes", "description", "New authored guidance"), ("stages", "description", "Changed criterion"))
        for section, field, value in cases:
            with self.subTest(section=section, field=field):
                rows = deepcopy(self.rows)
                target = rows[section][0] if isinstance(rows[section], list) else rows[section]
                target[field] = value
                self.assertNotEqual(original, _assemble_bundle(**rows)["revision"])

    def test_only_supported_stage_config_and_opaque_file_reference_are_exported(self):
        bundle = _assemble_bundle(**self.rows)
        stage = bundle["crm"]["pipelines"][0]["stages"][0]
        self.assertEqual(stage["config"], {"required_attribute_ids": [self.rows["attributes"][0]["id"]]})
        document = bundle["knowledge"]["documents"][0]
        self.assertEqual(document["file_reference"], {"document_id": "1", "filename": "brochure.pdf", "file_extension": ".pdf"})
        self.assertTrue(document["has_file"])
        self.assertEqual(document["source_url"], "https://example.com/pricing")
        self.assertFalse(document["is_active"])
        self.assertEqual(document["processing_status"], "pending")
        serialized = json.dumps(bundle)
        for private in ("user:password", "token=secret", "#private", "account-secret", "stage-secret", "nested-secret"):
            self.assertNotIn(private, serialized)

    def test_archived_and_sensitive_attributes_never_authorize_stage_requirements(self):
        for override in ({"is_active": False}, {"key": "api_token", "name": "API token"}):
            with self.subTest(override=override):
                rows = deepcopy(self.rows)
                rows["attributes"][0].update(override)
                bundle = _assemble_bundle(**rows)
                self.assertEqual(bundle["crm"]["pipelines"][0]["stages"][0]["config"], {"required_attribute_ids": []})

    def test_malformed_or_credential_bearing_source_urls_never_leak(self):
        cases = (("https://u:p@[2001:db8::1]:8443/path?access_token=x#secret", "https://[2001:db8::1]:8443/path"),
                 ("https://host:bad/path", ""), ("file:///private/config", ""),
                 ("javascript:alert(1)", ""), ("https://user:password@host/path?token=secret", "https://host/path"))
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(_safe_source_url(raw), expected)

    def test_stage_from_unlisted_pipeline_fails_closed(self):
        self.rows["stages"][0]["pipeline_id"] = str(uuid4())
        with self.assertRaises(TenantScopeError):
            _assemble_bundle(**self.rows)


class BrainBundleDatabaseTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Brain company", timezone="Asia/Kolkata",
                                               settings={"api_key": "organization-secret"})
        self.foreign = Organization.objects.create(name="Foreign company")
        self.info = OrgInfo.objects.create(organization=self.org, about="Company information",
            bot_languages="English, Hinglish", ai_playbook="## Rules\nOur rule.",
            qualification_model="qualification-model", sales_support_model="support-model", summary_model="summary-model")
        self.faq = FAQ.objects.create(organization=self.org, question="Price?", answer="2999 per month", is_active=False)
        FAQ.objects.create(organization=self.foreign, question="Foreign FAQ", answer="Foreign answer")
        self.attribute = AttributeDefinition.objects.create(organization=self.org, name="Goal", key="goal")
        AttributeDefinition.objects.create(organization=self.foreign, name="Foreign attribute", key="foreign")
        self.pipeline = Pipeline.objects.create(organization=self.org, name="Sales", phone_number="9000000000")
        foreign_pipeline = Pipeline.objects.create(organization=self.foreign, name="Foreign pipeline")
        self.stage = self.pipeline.stages.get(name="New leads")
        self.stage.config = {"required_attribute_ids": [str(self.attribute.pk)], "access_token": "stage-secret"}
        self.stage.save(update_fields=["config", "updated_at"])
        foreign_pipeline.stages.filter(name="New leads").update(description="Foreign stage criterion")
        self.source = KnowledgeSource.objects.create(organization=self.org, source_type="url", name="Pricing",
            url="https://user:password@example.com/pricing?token=secret", is_active=False)
        KnowledgeSource.objects.create(organization=self.foreign, source_type="url", name="Foreign source", url="https://foreign.example")
        self.document = Document.objects.create(organization=self.org, name="Brochure", version=2,
            file="private/account-secret/brochure.pdf", processing_status="pending", is_active=False,
            file_sharing_ready=True, share_instruction="Send on request.", processing_error="provider-secret")
        Document.objects.create(organization=self.foreign, name="Foreign document")
        Lead.objects.create(organization=self.org, pipeline=self.pipeline, stage=self.stage,
                            name="Private customer", phone="+919999999999", attributes={"private_fact": "customer-secret"})

    def test_complete_tenant_configuration_uses_eight_queries_and_never_reads_chunks_or_customers(self):
        with CaptureQueriesContext(connection) as queries:
            bundle = get_organization_ai_brain_bundle(organization=self.org)
        self.assertEqual(len(queries), 8)
        self.assertEqual(bundle["organization"]["id"], str(self.org.pk))
        self.assertEqual(bundle["faqs"][0]["id"], str(self.faq.pk))
        self.assertFalse(bundle["faqs"][0]["is_active"])
        self.assertEqual(bundle["knowledge"]["sources"][0]["id"], str(self.source.pk))
        self.assertEqual(bundle["knowledge"]["documents"][0]["processing_status"], "pending")
        self.assertEqual(bundle["ai"]["summary_model"], "summary-model")
        serialized = json.dumps(bundle)
        for private in ("Foreign", "organization-secret", "stage-secret", "provider-secret", "account-secret", "customer-secret", "Private customer"):
            self.assertNotIn(private, serialized)
        for query in queries:
            self.assertNotIn("ai_engagement_chunk", query["sql"])
            self.assertNotIn('"crm_lead"', query["sql"])

    def test_query_set_updates_without_timestamps_change_fresh_revision(self):
        first = get_organization_ai_brain_bundle(organization=self.org)
        Document.objects.filter(pk=self.document.pk).update(file_sharing_ready=False)
        second = get_organization_ai_brain_bundle(organization=self.org)
        self.assertNotEqual(first["revision"], second["revision"])
        FAQ.objects.filter(pk=self.faq.pk).update(answer="A changed exact price.")
        third = get_organization_ai_brain_bundle(organization=self.org)
        self.assertNotEqual(second["revision"], third["revision"])
        self.assertEqual(third["faqs"][0]["answer"], "A changed exact price.")

    def test_organization_identity_is_read_fresh_instead_of_stale_instance(self):
        Organization.objects.filter(pk=self.org.pk).update(name="Updated company")
        self.assertEqual(get_organization_ai_brain_bundle(organization=self.org)["organization"]["name"], "Updated company")

    def test_missing_or_nonexistent_organization_fails_closed(self):
        for org in (None, SimpleNamespace(id=uuid4())):
            with self.subTest(org=org):
                with self.assertRaises(TenantScopeError):
                    get_organization_ai_brain_bundle(organization=org)
