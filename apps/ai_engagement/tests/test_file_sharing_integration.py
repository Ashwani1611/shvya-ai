from __future__ import annotations

from unittest.mock import patch

import pytest

from apps.ai_engagement.models import Document
from apps.ai_engagement.services.file_sharing import (
    FileSharingService,
)
from apps.ai_engagement.services.context import (
    AIContextBuilder,
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


pytestmark = pytest.mark.django_db


def make_text_file(
    *,
    name: str = "pricing.txt",
    content: str = "Our pricing starts at ₹10,000.",
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(
        name=name,
        content=content.encode("utf-8"),
        content_type="text/plain",
    )


@pytest.fixture
def organization():
    return Organization.objects.create(
        name="File Sharing Integration Organization",
    )


@pytest.fixture
def pipeline(organization):
    return Pipeline.objects.create(
        organization=organization,
        name="File Sharing Pipeline",
        is_active=True,
    )


@pytest.fixture
def stage(pipeline):
    return Stage.objects.create(
        pipeline=pipeline,
        name="New",
        description="New lead.",
        display_order=0,
        is_active=True,
    )


@pytest.fixture
def account(organization):
    return WhatsAppAccount.objects.create(
        organization=organization,
        connection_type=(
            WhatsAppAccount.ConnectionType.API
        ),
        business_name="File Sharing Integration WhatsApp",
        status=(
            WhatsAppAccount.Status.CONNECTED
        ),
        is_active=True,
    )


@pytest.fixture
def lead(
    organization,
    pipeline,
    stage,
):
    return Lead.objects.create(
        organization=organization,
        pipeline=pipeline,
        stage=stage,
        name="Integration Lead",
        phone="+919876543299",
        lead_source="whatsapp_api",
    )


@pytest.fixture
def pricing_document(organization):
    return Document.objects.create(
        organization=organization,
        name="Pricing PDF",
        source_key="pricing.txt",
        version=1,
        file=make_text_file(),
        source_url="",
        processing_status=(
            Document.ProcessingStatus.COMPLETED
        ),
        processing_error="",
        is_active=True,
    )


def create_message(
    *,
    organization,
    account,
    lead,
):
    return WhatsAppMessage.objects.create(
        organization=organization,
        account=account,
        lead=lead,
        direction=WhatsAppMessage.Direction.INBOUND,
        external_id="file-sharing-integration-001",
        from_number=lead.phone,
        to_number="TEMP",
        body="Please send me the pricing.",
        status=WhatsAppMessage.Status.RECEIVED,
        raw_payload={
            "test": True,
        },
        is_read=True,
    )


class TestFileSharingIntegration:
    def test_context_contains_retrieved_knowledge_document(
        self,
        organization,
        lead,
        pricing_document,
        account,
    ):
        create_message(
            organization=organization,
            account=account,
            lead=lead,
        )

        knowledge_item = {
            "chunk_id": "chunk-1",
            "document_id": str(
                pricing_document.id
            ),
            "document_name": pricing_document.name,
            "document_version": pricing_document.version,
            "content": "Our pricing starts at ₹10,000.",
            "similarity": 0.97,
            "distance": 0.03,
        }

        with patch(
            "apps.ai_engagement.services.context."
            "KnowledgeRetrievalService.retrieve_by_vector",
            return_value=[],
        ):
            with patch.object(
                AIContextBuilder,
                "_build_knowledge_context",
                return_value=[
                    knowledge_item
                ],
            ):
                context = AIContextBuilder().build(
                    organization=organization,
                    lead=lead,
                    query_vector=[
                        0.0
                    ] * 1536,
                    message_limit=100,
                    knowledge_limit=5,
                )

        assert context.knowledge

        assert (
            context.knowledge[0]["document_id"]
            == str(pricing_document.id)
        )

    def test_file_candidate_is_built_from_context_knowledge(
        self,
        organization,
        lead,
        pricing_document,
    ):
        knowledge_item = {
            "chunk_id": "chunk-1",
            "document_id": str(
                pricing_document.id
            ),
            "document_name": pricing_document.name,
            "document_version": pricing_document.version,
            "content": "Our pricing starts at ₹10,000.",
            "similarity": 0.97,
            "distance": 0.03,
        }

        with patch.object(
            FileSharingService,
            "build_ai_context",
        ) as mocked_build_context:

            from apps.ai_engagement.services.context import (
                AIContext,
            )

            context = AIContext(
                organization={
                    "id": str(organization.id),
                    "name": organization.name,
                    "ai_enabled": True,
                    "about": "",
                    "bot_languages": "English",
                    "qualification_requirements": "",
                    "bump_up_enabled": False,
                    "bump_up_count": 0,
                },
                lead={
                    "id": str(lead.id),
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
                pipeline={},
                stage={},
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
                        }
                    ],
                },
                conversation_summary=None,
                qualification_notes=[],
                knowledge=[
                    knowledge_item
                ],
            )

            mocked_build_context.return_value = context

            candidates = (
                FileSharingService().build_file_candidates(
                    organization=organization,
                    context=context,
                )
            )

        assert len(candidates) == 1
        assert (
            candidates[0]["document_id"]
            == pricing_document.id
        )
        assert (
            candidates[0]["name"]
            == pricing_document.name
        )
        assert (
            candidates[0]["evidence"]
            == "Our pricing starts at ₹10,000."
        )

    def test_generate_selects_only_an_eligible_retrieved_document(
        self,
        organization,
        lead,
        pricing_document,
    ):
        knowledge_item = {
            "chunk_id": "chunk-1",
            "document_id": str(
                pricing_document.id
            ),
            "document_name": pricing_document.name,
            "document_version": pricing_document.version,
            "content": "Our pricing starts at ₹10,000.",
            "similarity": 0.97,
            "distance": 0.03,
        }

        with patch.object(
            FileSharingService,
            "build_ai_context",
        ) as mocked_build_context:

            from apps.ai_engagement.services.context import (
                AIContext,
            )

            mocked_build_context.return_value = AIContext(
                organization={
                    "id": str(organization.id),
                    "name": organization.name,
                    "ai_enabled": True,
                    "about": "",
                    "bot_languages": "English",
                    "qualification_requirements": "",
                    "bump_up_enabled": False,
                    "bump_up_count": 0,
                },
                lead={
                    "id": str(lead.id),
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
                pipeline={},
                stage={},
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
                        }
                    ],
                },
                conversation_summary=None,
                qualification_notes=[],
                knowledge=[
                    knowledge_item
                ],
            )

            with patch(
                "apps.ai_engagement.services.file_sharing."
                "OpenAIProvider"
            ) as mocked_provider:

                from apps.ai_engagement.services.ai_provider import (
                    AITextResult,
                )

                mocked_provider.return_value.generate_text.return_value = (
                    AITextResult(
                        text=(
                            "{"
                            f'"should_share": true, '
                            f'"document_id": {pricing_document.id}, '
                            '"reason": "The pricing file directly answers the lead\'s question."'
                            "}"
                        ),
                        model="gpt-4.1-nano",
                    )
                )

                decision = (
                    FileSharingService().generate(
                        organization=organization,
                        lead=lead,
                    )
                )

        assert decision.should_share is True
        assert (
            decision.document_id
            == pricing_document.id
        )