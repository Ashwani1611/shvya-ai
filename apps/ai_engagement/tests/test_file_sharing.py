from __future__ import annotations

import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from apps.ai_engagement.models import (
    Chunk,
    Document,
    InternalConversationSummary,
    OrgInfo,
)
from apps.ai_engagement.services.ai_provider import (
    AIProviderPermanentError,
    AIProviderTransientError,
    AITextResult,
)
from apps.ai_engagement.services.context import AIContext
from apps.ai_engagement.services.file_sharing import (
    FileSharingDecision,
    FileSharingError,
    FileSharingService,
)
from apps.channels.models import (
    WhatsAppAccount,
    WhatsAppMessage,
)
from apps.crm.models import (
    Lead,
    Pipeline,
    Stage,
)
from apps.organizations.models import Organization


class FileSharingServiceTests(TestCase):
    """
    Tests the AI-guided file-sharing service.

    The OpenAI provider is mocked.

    These tests do NOT call:
        - OpenAI
        - Meta
        - Celery
    """

    @classmethod
    def setUpTestData(cls):
        cls.organization = Organization.objects.create(
            name="File Sharing Test Organization",
        )

        cls.other_organization = Organization.objects.create(
            name="Other Organization",
        )

        OrgInfo.objects.create(
            organization=cls.organization,
            about=(
                "Cybersecurity training academy "
                "providing certification-focused programs."
            ),
            bot_languages="English, Hindi, Hinglish",
            qualification_requirements=(
                "Understand the lead's course and buying intent."
            ),
            ai_enabled=True,
        )

        cls.pipeline = Pipeline.objects.create(
            organization=cls.organization,
            name="File Sharing Pipeline",
            is_active=True,
        )

        cls.stage = Stage.objects.create(
            pipeline=cls.pipeline,
            name="New",
            description="New lead.",
            display_order=0,
            is_active=True,
        )

        cls.account = WhatsAppAccount.objects.create(
            organization=cls.organization,
            connection_type=(
                WhatsAppAccount.ConnectionType.API
            ),
            business_name="File Sharing WhatsApp",
            status=(
                WhatsAppAccount.Status.CONNECTED
            ),
            is_active=True,
        )

    # ========================================================
    # HELPERS
    # ========================================================

    def create_lead(
        self,
        phone: str = "+919876543200",
        name: str = "File Sharing Lead",
    ):
        return Lead.objects.create(
            organization=self.organization,
            pipeline=self.pipeline,
            stage=self.stage,
            name=name,
            phone=phone,
            lead_source="whatsapp_api",
        )

    def create_message(
        self,
        *,
        lead,
        external_id: str,
        body: str,
        direction=WhatsAppMessage.Direction.INBOUND,
    ):
        status = (
            WhatsAppMessage.Status.RECEIVED
            if direction
            == WhatsAppMessage.Direction.INBOUND
            else WhatsAppMessage.Status.SENT
        )

        return WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            lead=lead,
            direction=direction,
            external_id=external_id,
            from_number=lead.phone,
            to_number="TEMP",
            body=body,
            status=status,
            raw_payload={
                "test": True,
            },
            is_read=True,
        )

    def create_document(
        self,
        *,
        organization=None,
        name="Pricing PDF",
        is_active=True,
        processing_status=Document.ProcessingStatus.COMPLETED,
        with_file=True,
    ):
        organization = (
            organization
            or self.organization
        )

        uploaded_file = None

        if with_file:
            uploaded_file = SimpleUploadedFile(
                name="pricing.pdf",
                content=b"Pricing document content",
                content_type="application/pdf",
            )

        return Document.objects.create(
            organization=organization,
            name=name,
            source_key=name,
            version=1,
            file=uploaded_file,
            source_url="",
            processing_status=processing_status,
            processing_error="",
            is_active=is_active,
        )

    def create_chunk(
        self,
        *,
        document,
        content,
        similarity=0.95,
    ):
        """
        Create a chunk whose embedding is not required because
        candidate construction receives relevance from the context.
        """

        return Chunk.objects.create(
            document=document,
            organization=document.organization,
            content=content,
            chunk_index=0,
            is_active=True,
        )

    def build_context(
        self,
        *,
        knowledge,
        lead,
    ):
        return AIContext(
            organization={
                "id": str(
                    self.organization.id
                ),
                "name": self.organization.name,
                "ai_enabled": True,
                "about": "Test organization",
                "bot_languages": "English",
                "qualification_requirements": "",
                "bump_up_enabled": False,
                "bump_up_count": 0,
            },
            lead={
                "id": str(
                    lead.id
                ),
                "name": lead.name,
                "phone": lead.phone,
                "email": "",
                "notes": "",
                "attributes": {},
                "lead_source": lead.lead_source,
                "stage_entered_at": None,
                "created_at": None,
                "updated_at": None,
            },
            pipeline={
                "id": str(
                    self.pipeline.id
                ),
                "name": self.pipeline.name,
                "description": "",
                "country_code": "",
                "phone_number": "",
                "is_active": True,
            },
            stage={
                "id": str(
                    self.stage.id
                ),
                "name": self.stage.name,
                "description": self.stage.description,
                "display_order": 0,
                "is_active": True,
            },
            contacts=[],
            attributes=[],
            conversation={
                "message_count": 1,
                "messages": [
                    {
                        "id": "message-1",
                        "direction": "inbound",
                        "speaker": "lead",
                        "body": "Please send me the pricing.",
                        "status": "received",
                        "created_at": None,
                    },
                ],
            },
            conversation_summary=None,
            qualification_notes=[],
            knowledge=knowledge,
        )

    # ========================================================
    # ELIGIBLE DOCUMENTS
    # ========================================================

    def test_get_eligible_documents_returns_active_completed_files(
        self,
    ):
        eligible = self.create_document(
            name="Eligible Pricing PDF",
        )

        self.create_document(
            name="Inactive Pricing PDF",
            is_active=False,
        )

        self.create_document(
            name="Pending Pricing PDF",
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
        )

        self.create_document(
            name="URL Only",
            with_file=False,
        )

        documents = (
            FileSharingService().get_eligible_documents(
                organization=self.organization,
            )
        )

        document_ids = {
            document.id
            for document in documents
        }

        self.assertIn(
            eligible.id,
            document_ids,
        )

        self.assertEqual(
            len(documents),
            1,
        )

    def test_get_eligible_documents_is_organization_scoped(
        self,
    ):
        other_document = self.create_document(
            organization=self.other_organization,
            name="Other Organization File",
        )

        documents = (
            FileSharingService().get_eligible_documents(
                organization=self.organization,
            )
        )

        document_ids = {
            document.id
            for document in documents
        }

        self.assertNotIn(
            other_document.id,
            document_ids,
        )

    def test_get_eligible_documents_can_filter_by_ids(
        self,
    ):
        first = self.create_document(
            name="First File",
        )

        second = self.create_document(
            name="Second File",
        )

        documents = (
            FileSharingService().get_eligible_documents(
                organization=self.organization,
                document_ids={
                    first.id,
                },
            )
        )

        self.assertEqual(
            [document.id for document in documents],
            [first.id],
        )

        self.assertNotIn(
            second.id,
            [
                document.id
                for document in documents
            ],
        )

    # ========================================================
    # CANDIDATE BUILDING
    # ========================================================

    def test_build_file_candidates_groups_duplicate_document(
        self,
    ):
        lead = self.create_lead()

        document = self.create_document(
            name="Pricing PDF",
        )

        knowledge = [
            {
                "chunk_id": "1",
                "document_id": str(
                    document.id
                ),
                "document_name": document.name,
                "document_version": document.version,
                "content": "Pricing starts at ₹10,000.",
                "similarity": 0.96,
                "distance": 0.04,
            },
            {
                "chunk_id": "2",
                "document_id": str(
                    document.id
                ),
                "document_name": document.name,
                "document_version": document.version,
                "content": "Weekend batch pricing information.",
                "similarity": 0.92,
                "distance": 0.08,
            },
        ]

        context = self.build_context(
            knowledge=knowledge,
            lead=lead,
        )

        candidates = (
            FileSharingService().build_file_candidates(
                organization=self.organization,
                context=context,
            )
        )

        self.assertEqual(
            len(candidates),
            1,
        )

        self.assertEqual(
            candidates[0]["document_id"],
            document.id,
        )

    def test_build_file_candidates_excludes_ineligible_documents(
        self,
    ):
        lead = self.create_lead()

        inactive_document = self.create_document(
            name="Inactive PDF",
            is_active=False,
        )

        pending_document = self.create_document(
            name="Pending PDF",
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
        )

        url_only_document = self.create_document(
            name="URL Only",
            with_file=False,
        )

        knowledge = [
            {
                "chunk_id": "1",
                "document_id": str(
                    inactive_document.id
                ),
                "document_name": inactive_document.name,
                "document_version": inactive_document.version,
                "content": "Inactive file",
                "similarity": 0.99,
                "distance": 0.01,
            },
            {
                "chunk_id": "2",
                "document_id": str(
                    pending_document.id
                ),
                "document_name": pending_document.name,
                "document_version": pending_document.version,
                "content": "Pending file",
                "similarity": 0.98,
                "distance": 0.02,
            },
            {
                "chunk_id": "3",
                "document_id": str(
                    url_only_document.id
                ),
                "document_name": url_only_document.name,
                "document_version": url_only_document.version,
                "content": "URL source",
                "similarity": 0.97,
                "distance": 0.03,
            },
        ]

        context = self.build_context(
            knowledge=knowledge,
            lead=lead,
        )

        candidates = (
            FileSharingService().build_file_candidates(
                organization=self.organization,
                context=context,
            )
        )

        self.assertEqual(
            candidates,
            [],
        )

    # ========================================================
    # PROVIDER INPUT
    # ========================================================

    def test_provider_input_includes_file_candidates(
        self,
    ):
        lead = self.create_lead()

        document = self.create_document(
            name="Pricing PDF",
        )

        with patch.object(
            FileSharingService,
            "build_ai_context",
        ) as mocked_build_context:

            mocked_build_context.return_value = (
                self.build_context(
                    knowledge=[
                        {
                            "chunk_id": "1",
                            "document_id": str(
                                document.id
                            ),
                            "document_name": document.name,
                            "document_version": document.version,
                            "content": (
                                "Pricing starts at ₹10,000."
                            ),
                            "similarity": 0.95,
                            "distance": 0.05,
                        },
                    ],
                    lead=lead,
                )
            )

            provider_input = (
                FileSharingService().build_provider_input(
                    organization=self.organization,
                    lead=lead,
                )
            )

        self.assertIn(
            "FILE CANDIDATES",
            provider_input,
        )

        self.assertIn(
            str(document.id),
            provider_input,
        )

        self.assertIn(
            "Pricing PDF",
            provider_input,
        )

        self.assertIn(
            "Pricing starts at ₹10,000.",
            provider_input,
        )

    # ========================================================
    # PARSING
    # ========================================================

    def test_parse_no_share_decision(
        self,
    ):
        lead = self.create_lead()

        decision = (
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": False,
                        "document_id": None,
                        "reason": (
                            "No file is sufficiently relevant."
                        ),
                    }
                ),
                model="gpt-4.1-nano",
            )
        )

        self.assertEqual(
            decision,
            FileSharingDecision(
                should_share=False,
                document_id=None,
                reason="No file is sufficiently relevant.",
                model="gpt-4.1-nano",
            ),
        )

    def test_parse_share_decision_for_eligible_document(
        self,
    ):
        lead = self.create_lead()

        document = self.create_document(
            name="Pricing PDF",
        )

        decision = (
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": True,
                        "document_id": document.id,
                        "reason": (
                            "The pricing document directly "
                            "answers the lead's question."
                        ),
                    }
                ),
                model="gpt-4.1-nano",
            )
        )

        self.assertEqual(
            decision.should_share,
            True,
        )

        self.assertEqual(
            decision.document_id,
            document.id,
        )

    def test_parse_rejects_invalid_json(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "not valid JSON",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text="not-json",
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_non_object_json(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "must be a JSON object",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text="[]",
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_invalid_schema(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "invalid schema",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": False,
                        "document_id": None,
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_non_boolean_should_share(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "should_share must be a boolean",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": "true",
                        "document_id": None,
                        "reason": "Test",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_document_when_should_not_share(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "document_id must be null",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": False,
                        "document_id": 123,
                        "reason": "Test",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_missing_document_when_should_share(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "document_id must be an integer",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": True,
                        "document_id": None,
                        "reason": "Test",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_hallucinated_document_id(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "not an eligible",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": True,
                        "document_id": 999999,
                        "reason": "Invented file",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_document_from_another_organization(
        self,
    ):
        lead = self.create_lead()

        other_document = self.create_document(
            organization=self.other_organization,
            name="Other Organization PDF",
        )

        with self.assertRaisesMessage(
            FileSharingError,
            "not an eligible",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": True,
                        "document_id": other_document.id,
                        "reason": "Cross organization attempt",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_inactive_document(
        self,
    ):
        lead = self.create_lead()

        document = self.create_document(
            name="Inactive PDF",
            is_active=False,
        )

        with self.assertRaisesMessage(
            FileSharingError,
            "not an eligible",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": True,
                        "document_id": document.id,
                        "reason": "Inactive file",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_incomplete_document(
        self,
    ):
        lead = self.create_lead()

        document = self.create_document(
            name="Pending PDF",
            processing_status=(
                Document.ProcessingStatus.PENDING
            ),
        )

        with self.assertRaisesMessage(
            FileSharingError,
            "not an eligible",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": True,
                        "document_id": document.id,
                        "reason": "Pending file",
                    }
                ),
                model="gpt-4.1-nano",
            )

    def test_parse_rejects_empty_reason(
        self,
    ):
        lead = self.create_lead()

        with self.assertRaisesMessage(
            FileSharingError,
            "reason cannot be empty",
        ):
            FileSharingService().parse_decision(
                organization=self.organization,
                lead=lead,
                raw_text=json.dumps(
                    {
                        "should_share": False,
                        "document_id": None,
                        "reason": "",
                    }
                ),
                model="gpt-4.1-nano",
            )

    # ========================================================
    # PROVIDER ERRORS
    # ========================================================

    @patch(
        "apps.ai_engagement.services.file_sharing.OpenAIProvider"
    )
    def test_generate_propagates_transient_provider_error(
        self,
        mocked_provider,
    ):
        lead = self.create_lead()

        mocked_provider.return_value.generate_text.side_effect = (
            AIProviderTransientError(
                "temporary provider failure"
            )
        )

        with self.assertRaises(
            AIProviderTransientError
        ):
            FileSharingService().generate(
                organization=self.organization,
                lead=lead,
            )

    @patch(
        "apps.ai_engagement.services.file_sharing.OpenAIProvider"
    )
    def test_generate_wraps_permanent_provider_error(
        self,
        mocked_provider,
    ):
        lead = self.create_lead()

        mocked_provider.return_value.generate_text.side_effect = (
            AIProviderPermanentError(
                "provider rejected request"
            )
        )

        with self.assertRaisesMessage(
            FileSharingError,
            "AI file-sharing generation failed",
        ):
            FileSharingService().generate(
                organization=self.organization,
                lead=lead,
            )

    # ========================================================
    # GENERATION
    # ========================================================

    @patch(
        "apps.ai_engagement.services.file_sharing.OpenAIProvider"
    )
    def test_generate_returns_valid_share_decision(
        self,
        mocked_provider,
    ):
        lead = self.create_lead()

        document = self.create_document(
            name="Pricing PDF",
        )

        with patch.object(
            FileSharingService,
            "build_ai_context",
        ) as mocked_build_context:

            mocked_build_context.return_value = (
                self.build_context(
                    knowledge=[
                        {
                            "chunk_id": "1",
                            "document_id": str(
                                document.id
                            ),
                            "document_name": document.name,
                            "document_version": document.version,
                            "content": (
                                "Pricing starts at ₹10,000."
                            ),
                            "similarity": 0.97,
                            "distance": 0.03,
                        },
                    ],
                    lead=lead,
                )
            )

            mocked_provider.return_value.generate_text.return_value = (
                AITextResult(
                    text=json.dumps(
                        {
                            "should_share": True,
                            "document_id": document.id,
                            "reason": (
                                "The pricing PDF directly "
                                "answers the lead's question."
                            ),
                        }
                    ),
                    model="gpt-4.1-nano",
                )
            )

            decision = (
                FileSharingService().generate(
                    organization=self.organization,
                    lead=lead,
                )
            )

        self.assertTrue(
            decision.should_share
        )

        self.assertEqual(
            decision.document_id,
            document.id,
        )

        mocked_provider.return_value.generate_text.assert_called_once()

        call_kwargs = (
            mocked_provider.return_value
            .generate_text
            .call_args.kwargs
        )

        self.assertEqual(
            call_kwargs["metadata"]["purpose"],
            "ai_file_sharing_decision",
        )

    @patch(
        "apps.ai_engagement.services.file_sharing.OpenAIProvider"
    )
    def test_generate_returns_valid_no_share_decision(
        self,
        mocked_provider,
    ):
        lead = self.create_lead()

        with patch.object(
            FileSharingService,
            "build_ai_context",
        ) as mocked_build_context:

            mocked_build_context.return_value = (
                self.build_context(
                    knowledge=[],
                    lead=lead,
                )
            )

            mocked_provider.return_value.generate_text.return_value = (
                AITextResult(
                    text=json.dumps(
                        {
                            "should_share": False,
                            "document_id": None,
                            "reason": (
                                "No available file is relevant "
                                "enough to share."
                            ),
                        }
                    ),
                    model="gpt-4.1-nano",
                )
            )

            decision = (
                FileSharingService().generate(
                    organization=self.organization,
                    lead=lead,
                )
            )

        self.assertFalse(
            decision.should_share
        )

        self.assertIsNone(
            decision.document_id
        )