from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.ai_engagement.models import (
    AITrace,
    Chunk,
    Document,
    InternalConversationSummary,
    OrgInfo,
)
from apps.ai_engagement.services.context import AIContextBuilder, AIContextError
from apps.ai_engagement.services.crm_executor import (
    CRMActionExecutionError,
    CRMActionExecutor,
)
from apps.ai_engagement.services.file_sharing import (
    FileSharingError,
    FileSharingService,
)
from apps.ai_engagement.services.organization_runtime_profile import (
    OrganizationAIRuntimeProfileBuilder,
    get_organization_ai_runtime_profile,
)
from apps.ai_engagement.services.retrieval import KnowledgeRetrievalService
from apps.ai_engagement.services.tenant_guard import (
    OBJECT_NOT_IN_ORGANIZATION,
    TENANT_SCOPE_MISMATCH,
    TenantGuard,
    TenantScopeError,
)
from apps.ai_engagement.services.trace_service import begin_trace, flush
from apps.ai_engagement.services.transactional_turn_runtime import (
    _requirements_for_turn,
)
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import (
    AttributeDefinition,
    Lead,
    LeadContact,
    LeadNote,
    LeadReminder,
    Pipeline,
    Stage,
)
from apps.organizations.models import Organization


class Phase4TenantRuntimeTests(TestCase):
    """Acceptance/security coverage for the Phase 4 organization runtime boundary."""

    @classmethod
    def setUpTestData(cls):
        cls.org_a = Organization.objects.create(
            name="Apex Fitness",
            settings={
                "pricing": {"membership": "₹X"},
                "policies": {"refund": "Gym refund policy"},
                "locations": ["Location A"],
                "services": ["Fitness coaching"],
                "tone": "concise and energetic",
                "language_rules": {"default": "English"},
                "api_token": "ORG_A_SETTINGS_SECRET",
                "ai": {
                    "response_mode": "concise",
                    "provider_token": "NESTED_ORG_A_SECRET",
                },
            },
        )
        cls.org_b = Organization.objects.create(
            name="Beacon University",
            settings={
                "pricing": {"tuition": "₹Y"},
                "policies": {"refund": "University refund policy"},
                "locations": ["Location B"],
                "services": ["Admissions counselling"],
                "tone": "formal and helpful",
                "language_rules": {"default": "English"},
                "api_token": "ORG_B_SETTINGS_SECRET",
            },
        )
        OrgInfo.objects.create(
            organization=cls.org_a,
            about="Gym business information for Apex Fitness.",
            bot_languages="English, Hindi",
            qualification_requirements=(
                "What is your fitness goal?\n"
                "When would you like to start training?"
            ),
            engagement_instructions="Use the approved gym information only.",
        )
        OrgInfo.objects.create(
            organization=cls.org_b,
            about="University admissions information for Beacon University.",
            bot_languages="English",
            qualification_requirements=(
                "Which course are you interested in?\n"
                "Which admission intake are you targeting?"
            ),
            engagement_instructions="Use the approved admissions information only.",
        )

        cls.pipeline_a = Pipeline.objects.create(
            organization=cls.org_a,
            name="Gym Sales",
            country_code="+91",
            phone_number="9000000001",
        )
        cls.pipeline_b = Pipeline.objects.create(
            organization=cls.org_b,
            name="Admissions",
            country_code="+91",
            phone_number="9000000002",
        )
        cls.stage_a = cls.pipeline_a.stages.get(name="New leads")
        cls.stage_b = cls.pipeline_b.stages.get(name="New leads")
        cls.stage_a_next = Stage.objects.create(
            pipeline=cls.pipeline_a,
            name="Trial Requested",
            display_order=50,
            is_active=True,
        )

        cls.lead_a = Lead.objects.create(
            organization=cls.org_a,
            pipeline=cls.pipeline_a,
            stage=cls.stage_a,
            name="Gym Lead",
            phone="+919111111111",
        )
        cls.lead_b = Lead.objects.create(
            organization=cls.org_b,
            pipeline=cls.pipeline_b,
            stage=cls.stage_b,
            name="University Lead",
            phone="+919222222222",
        )

        cls.attribute_a = AttributeDefinition.objects.create(
            organization=cls.org_a,
            name="Fitness Goal",
            key="fitness_goal",
        )
        cls.attribute_b = AttributeDefinition.objects.create(
            organization=cls.org_b,
            name="Admission Program",
            key="admission_program",
        )
        cls.contact_a = LeadContact.objects.create(
            lead=cls.lead_a,
            channel="whatsapp",
            handle=cls.lead_a.phone,
        )
        cls.contact_b = LeadContact.objects.create(
            lead=cls.lead_b,
            channel="whatsapp",
            handle=cls.lead_b.phone,
        )

        cls.account_a_api = WhatsAppAccount.objects.create(
            organization=cls.org_a,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Apex API",
            phone_number_id="provider-phone-a",
            waba_id="provider-waba-a",
            display_phone_number="+919000000001",
            access_token="ORG_A_META_SECRET",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        cls.account_a_hosted = WhatsAppAccount.objects.create(
            organization=cls.org_a,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Apex Hosted",
            phone_number_id="provider-hosted-a",
            display_phone_number="+919000000011",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        cls.account_b = WhatsAppAccount.objects.create(
            organization=cls.org_b,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Beacon API",
            phone_number_id="provider-phone-b",
            display_phone_number="+919000000002",
            access_token="ORG_B_META_SECRET",
            status=WhatsAppAccount.Status.CONNECTED,
        )

        cls.message_a = WhatsAppMessage.objects.create(
            organization=cls.org_a,
            account=cls.account_a_api,
            lead=cls.lead_a,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="phase4-a",
            from_number=cls.lead_a.phone,
            to_number=cls.account_a_api.display_phone_number,
            body="What does the gym membership cost?",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        cls.message_b = WhatsAppMessage.objects.create(
            organization=cls.org_b,
            account=cls.account_b,
            lead=cls.lead_b,
            direction=WhatsAppMessage.Direction.INBOUND,
            external_id="phase4-b",
            from_number=cls.lead_b.phone,
            to_number=cls.account_b.display_phone_number,
            body="What is the university tuition?",
            status=WhatsAppMessage.Status.RECEIVED,
        )

        cls.document_a = Document.objects.create(
            organization=cls.org_a,
            name="Apex Pricing",
            source_key="https://gym.example/pricing?token=SOURCE_SECRET",
            source_url="https://gym.example/pricing?token=SOURCE_SECRET",
            file="ai_knowledge/apex-pricing.pdf",
            share_instruction="Share only when a lead asks for gym pricing.",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        cls.document_b = Document.objects.create(
            organization=cls.org_b,
            name="Beacon Admissions",
            source_key="beacon-admissions.pdf",
            file="ai_knowledge/beacon-admissions.pdf",
            share_instruction="Share only for university admissions.",
            processing_status=Document.ProcessingStatus.COMPLETED,
            is_active=True,
        )
        cls.chunk_a = Chunk.objects.create(
            document=cls.document_a,
            organization=cls.org_a,
            content="Apex gym membership costs ₹X at Location A.",
            chunk_index=0,
        )
        cls.chunk_b = Chunk.objects.create(
            document=cls.document_b,
            organization=cls.org_b,
            content="Beacon university tuition costs ₹Y at Location B.",
            chunk_index=0,
        )

        cls.note_b = LeadNote.objects.create(
            lead=cls.lead_b,
            note="ORG B private note",
        )
        cls.reminder_b = LeadReminder.objects.create(
            lead=cls.lead_b,
            title="ORG B reminder",
            due_at=timezone.now() + timedelta(days=1),
        )
        cls.summary_b = InternalConversationSummary.objects.create(
            organization=cls.org_b,
            lead=cls.lead_b,
            summary="ORG B private conversation summary",
            source_message_count=1,
            source_last_message_id=cls.message_b.id,
        )
        cls.trace_b = AITrace.objects.create(
            organization=cls.org_b,
            lead=cls.lead_b,
            pipeline_id=cls.pipeline_b.id,
            stage_id=cls.stage_b.id,
            whatsapp_account_id=cls.account_b.id,
            source_inbound_message_id=cls.message_b.id,
            connection_type=AITrace.ConnectionType.API,
        )

    def test_runtime_profile_is_immutable_serializable_bounded_and_isolated(self):
        profile = OrganizationAIRuntimeProfileBuilder().build(
            organization=self.org_a,
            lead=self.lead_a,
        )
        payload = profile.as_dict()
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)

        self.assertEqual(profile.organization_id, str(self.org_a.id))
        self.assertEqual(payload["identity"]["name"], "Apex Fitness")
        self.assertIn("₹X", serialized)
        self.assertIn("Location A", serialized)
        self.assertIn("fitness", serialized.casefold())
        self.assertNotIn("₹Y", serialized)
        self.assertNotIn("Location B", serialized)
        self.assertNotIn("Admission Program", serialized)
        self.assertNotIn("Beacon", serialized)
        self.assertNotIn(self.chunk_a.content, serialized)
        self.assertNotIn(self.chunk_b.content, serialized)
        self.assertNotIn("ORG_A_SETTINGS_SECRET", serialized)
        self.assertNotIn("NESTED_ORG_A_SECRET", serialized)
        self.assertNotIn("ORG_A_META_SECRET", serialized)
        self.assertNotIn("provider-phone-a", serialized)
        self.assertNotIn("provider-waba-a", serialized)
        self.assertNotIn("SOURCE_SECRET", serialized)
        self.assertLess(len(serialized), 250_000)
        with self.assertRaises(TypeError):
            profile.identity["name"] = "mutated"

    def test_conflicting_organizations_compile_without_interference(self):
        profile_a = get_organization_ai_runtime_profile(
            organization=self.org_a,
            lead=self.lead_a,
        )
        profile_b = get_organization_ai_runtime_profile(
            organization=self.org_b,
            lead=self.lead_b,
        )
        serialized_a = json.dumps(profile_a.as_dict(), ensure_ascii=False)
        serialized_b = json.dumps(profile_b.as_dict(), ensure_ascii=False)

        self.assertIn("Gym Sales", serialized_a)
        self.assertIn("Fitness Goal", serialized_a)
        self.assertIn("₹X", serialized_a)
        self.assertNotIn("Admissions", serialized_a)
        self.assertNotIn("₹Y", serialized_a)

        self.assertIn("Admissions", serialized_b)
        self.assertIn("Admission Program", serialized_b)
        self.assertIn("₹Y", serialized_b)
        self.assertNotIn("Gym Sales", serialized_b)
        self.assertNotIn("₹X", serialized_b)

    def test_profile_rejects_mismatched_lead_and_is_transport_neutral(self):
        with self.assertRaises(TenantScopeError) as caught:
            get_organization_ai_runtime_profile(
                organization=self.org_a,
                lead=self.lead_b,
            )
        self.assertEqual(caught.exception.code, TENANT_SCOPE_MISMATCH)
        self.assertNotIn("Beacon", str(caught.exception))

        profile = get_organization_ai_runtime_profile(
            organization=self.org_a,
            lead=self.lead_a,
        )
        account_types = {
            item["connection_type"]
            for item in profile.as_dict()["channels"]["whatsapp_accounts"]
        }
        self.assertIn(WhatsAppAccount.ConnectionType.API, account_types)
        self.assertIn(WhatsAppAccount.ConnectionType.coexisted, account_types)

    def test_profile_loader_has_bounded_configuration_query_count(self):
        lead = Lead.objects.select_related(
            "organization",
            "pipeline",
            "stage",
        ).get(pk=self.lead_a.pk)
        with CaptureQueriesContext(connection) as captured:
            OrganizationAIRuntimeProfileBuilder().build(
                organization=lead.organization,
                lead=lead,
            )
        self.assertLessEqual(len(captured), 7)

    def test_context_and_qualification_use_only_current_organization_profile(self):
        context = AIContextBuilder().build(
            organization=self.org_a,
            lead=self.lead_a,
        )
        serialized = json.dumps(context.as_dict(), ensure_ascii=False)
        self.assertIn("Apex Fitness", serialized)
        self.assertNotIn("Beacon university tuition", serialized)
        self.assertNotIn("University Lead", serialized)
        self.assertNotIn("ORG B private", serialized)

        requirements_a = _requirements_for_turn(
            organization=self.org_a,
            lead=self.lead_a,
        )
        requirements_b = _requirements_for_turn(
            organization=self.org_b,
            lead=self.lead_b,
        )
        req_a = json.dumps(requirements_a, ensure_ascii=False).casefold()
        req_b = json.dumps(requirements_b, ensure_ascii=False).casefold()
        self.assertIn("fitness", req_a)
        self.assertNotIn("admission", req_a)
        self.assertIn("admission", req_b)
        self.assertNotIn("fitness", req_b)

        with self.assertRaises(AIContextError) as caught:
            AIContextBuilder().build(
                organization=self.org_a,
                lead=self.lead_b,
            )
        self.assertEqual(str(caught.exception), TENANT_SCOPE_MISMATCH)

    def test_tenant_guard_rejects_cross_tenant_objects_without_foreign_details(self):
        guard = TenantGuard(self.org_a)
        cases = (
            ("pipeline", lambda: guard.validate_pipeline(self.pipeline_b)),
            ("stage", lambda: guard.validate_stage(self.stage_b)),
            ("whatsapp_account", lambda: guard.validate_whatsapp_account(self.account_b)),
            ("contact", lambda: guard.validate_contact(self.contact_b, lead=self.lead_a)),
            ("attribute", lambda: guard.validate_attribute(self.attribute_b)),
            ("document", lambda: guard.validate_document(self.document_b)),
            ("chunk", lambda: guard.validate_chunk(self.chunk_b)),
            ("file", lambda: guard.validate_file(self.document_b)),
            ("note", lambda: guard.validate_note(self.note_b, lead=self.lead_a)),
            ("reminder", lambda: guard.validate_reminder(self.reminder_b, lead=self.lead_a)),
            ("summary", lambda: guard.validate_summary(self.summary_b, lead=self.lead_a)),
            ("trace", lambda: guard.validate_trace(self.trace_b, lead=self.lead_a)),
            (
                "message",
                lambda: guard.validate_message(
                    self.message_b,
                    lead=self.lead_a,
                    account=self.account_b,
                ),
            ),
        )
        for label, call in cases:
            with self.subTest(label=label):
                with self.assertRaises(TenantScopeError) as caught:
                    call()
                self.assertIn(
                    caught.exception.code,
                    {TENANT_SCOPE_MISMATCH, OBJECT_NOT_IN_ORGANIZATION},
                )
                self.assertNotIn(self.org_b.name, str(caught.exception))

    def test_rag_filters_tenant_before_ranking_and_guard_rejects_foreign_chunk(self):
        results = KnowledgeRetrievalService().retrieve_by_keyword(
            organization=self.org_a,
            query_text="Beacon university tuition",
            limit=10,
        )
        self.assertEqual(results, [])
        with self.assertRaises(TenantScopeError):
            TenantGuard(self.org_a).validate_chunk(self.chunk_b)

    def test_crm_guard_preserves_valid_actions_and_blocks_cross_tenant_targets(self):
        executor = CRMActionExecutor()

        executor.execute(
            organization=self.org_a,
            lead=self.lead_a,
            actions=[
                {
                    "type": "attribute_updates",
                    "updates": [{"key": "fitness_goal", "value": "Strength"}],
                }
            ],
        )
        self.lead_a.refresh_from_db()
        self.assertEqual(self.lead_a.attributes.get("fitness_goal"), "Strength")

        executor.execute(
            organization=self.org_a,
            lead=self.lead_a,
            actions=[
                {
                    "type": "contact_updates",
                    "updates": [
                        {
                            "contact_id": str(self.contact_a.id),
                            "channel": "whatsapp",
                            "handle": "+919333333333",
                        }
                    ],
                }
            ],
        )
        self.contact_a.refresh_from_db()
        self.assertEqual(self.contact_a.handle, "+919333333333")

        executor.execute(
            organization=self.org_a,
            lead=self.lead_a,
            actions=[
                {
                    "type": "pipeline_transition",
                    "stage_shift": {"stage_id": str(self.stage_a_next.id)},
                }
            ],
        )
        self.lead_a.refresh_from_db()
        self.assertEqual(self.lead_a.stage_id, self.stage_a_next.id)

        with self.assertRaises(CRMActionExecutionError) as stage_error:
            executor.execute(
                organization=self.org_a,
                lead=self.lead_a,
                actions=[
                    {
                        "type": "pipeline_transition",
                        "stage_shift": {"stage_id": str(self.stage_b.id)},
                    }
                ],
            )
        self.assertEqual(str(stage_error.exception), OBJECT_NOT_IN_ORGANIZATION)

        with self.assertRaises(CRMActionExecutionError) as contact_error:
            executor.execute(
                organization=self.org_a,
                lead=self.lead_a,
                actions=[
                    {
                        "type": "contact_updates",
                        "updates": [
                            {
                                "contact_id": str(self.contact_b.id),
                                "channel": "whatsapp",
                                "handle": "+919444444444",
                            }
                        ],
                    }
                ],
            )
        self.assertEqual(str(contact_error.exception), OBJECT_NOT_IN_ORGANIZATION)

        with self.assertRaises(CRMActionExecutionError) as attribute_error:
            executor.execute(
                organization=self.org_a,
                lead=self.lead_a,
                actions=[
                    {
                        "type": "attribute_updates",
                        "updates": [{"key": "admission_program", "value": "MBA"}],
                    }
                ],
            )
        self.assertEqual(str(attribute_error.exception), OBJECT_NOT_IN_ORGANIZATION)

    def test_file_selection_is_tenant_scoped(self):
        service = FileSharingService()
        allowed = service.get_eligible_documents(
            organization=self.org_a,
            document_ids={self.document_a.id},
        )
        self.assertEqual([document.id for document in allowed], [self.document_a.id])

        with self.assertRaises(FileSharingError) as caught:
            service.get_eligible_documents(
                organization=self.org_a,
                document_ids={self.document_b.id},
            )
        self.assertEqual(str(caught.exception), TENANT_SCOPE_MISMATCH)
        self.assertNotIn(self.org_b.name, str(caught.exception))

    def test_trace_tenant_failure_is_fail_closed_but_trace_storage_failure_is_fail_soft(self):
        with self.assertRaises(TenantScopeError):
            begin_trace(
                organization=self.org_a,
                lead=self.lead_a,
                source_message=self.message_b,
                account=self.account_b,
            )

        with patch(
            "apps.ai_engagement.models.AITrace.objects.create",
            side_effect=RuntimeError("simulated trace database failure"),
        ):
            token = begin_trace(
                organization=self.org_a,
                lead=self.lead_a,
                source_message=self.message_a,
                account=self.account_a_api,
            )
            # Phase 1 observability failure must still not abort the valid turn.
            flush(reset_token=token)

    def test_stage_must_belong_to_expected_pipeline_even_inside_same_organization(self):
        other_pipeline = Pipeline.objects.create(
            organization=self.org_a,
            name="Secondary Gym Pipeline",
        )
        other_stage = other_pipeline.stages.get(name="New leads")
        guard = TenantGuard(self.org_a)
        with self.assertRaises(TenantScopeError) as caught:
            guard.validate_stage(
                other_stage,
                pipeline=self.pipeline_a,
            )
        self.assertEqual(caught.exception.code, TENANT_SCOPE_MISMATCH)
