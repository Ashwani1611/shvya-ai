from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings

from apps.ai_engagement.models import (
    AICreditReservation,
    AICreditTransaction,
)
from apps.ai_engagement.services.ai_provider import (
    AIProviderPermanentError,
    OpenAIProvider,
)
from apps.ai_engagement.services.credits import (
    AICreditError,
    AICreditService,
    AICreditUnavailableError,
)
from apps.ai_engagement.services.embeddings import (
    EmbeddingError,
    EmbeddingService,
)
from apps.organizations.models import Organization


@patch.dict(
    os.environ,
    {
        "AI_CREDIT_MODEL_RATES_JSON": "",
        "AI_CREDIT_RESERVED_OUTPUT_TOKENS": "1000",
    },
)
class AICreditServiceTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="AI Credit Test Organization",
        )

    def test_new_organization_wallet_starts_at_zero(self):
        wallet = AICreditService.ensure_wallet(self.organization)

        self.assertEqual(wallet.balance, 0)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(wallet.available_credits, 0)
        self.assertFalse(wallet.can_use_ai)
        self.assertEqual(wallet.transactions.count(), 0)

        with self.assertRaises(AICreditUnavailableError):
            AICreditService.reserve_text(
                organization_id=self.organization.id,
                model="gpt-4.1-nano",
                instructions="Reply helpfully.",
                input_text="Hello",
                feature="engagement",
            )

    def test_manual_credit_is_audited_and_does_not_come_from_plan(self):
        wallet = AICreditService.add_manual_credits(
            organization=self.organization,
            amount=10000,
            reason="Initial manual AI allocation",
        )

        self.assertEqual(wallet.balance, 10000)
        self.assertEqual(wallet.lifetime_credits_added, 10000)

        transaction = wallet.transactions.get()
        self.assertEqual(
            transaction.transaction_type,
            AICreditTransaction.TransactionType.MANUAL_CREDIT,
        )
        self.assertEqual(transaction.amount, 10000)
        self.assertEqual(transaction.balance_after, 10000)
        self.assertEqual(
            transaction.description,
            "Initial manual AI allocation",
        )

    def test_reservation_prevents_concurrent_overspend_then_settles_actual_usage(self):
        AICreditService.add_manual_credits(
            organization=self.organization,
            amount=20,
            reason="Test funding",
        )

        reservation = AICreditService.reserve_text(
            organization_id=self.organization.id,
            model="gpt-4.1-nano",
            instructions="Reply helpfully.",
            input_text="Hello",
            feature="engagement",
            reference_id="lead-1",
        )

        wallet = AICreditService.ensure_wallet(self.organization)
        wallet.refresh_from_db()
        self.assertEqual(reservation.reserved_credits, 5)
        self.assertEqual(wallet.balance, 20)
        self.assertEqual(wallet.reserved_credits, 5)
        self.assertEqual(wallet.available_credits, 15)

        AICreditService.settle(
            reservation=reservation,
            input_tokens=1200,
            output_tokens=100,
        )

        wallet.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(
            reservation.status,
            AICreditReservation.Status.SETTLED,
        )
        self.assertEqual(reservation.actual_credits, 6)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(wallet.balance, 14)
        self.assertEqual(wallet.lifetime_credits_used, 6)

        usage = wallet.transactions.get(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
        )
        self.assertEqual(usage.amount, -6)
        self.assertEqual(usage.balance_after, 14)
        self.assertEqual(usage.feature, "engagement")
        self.assertEqual(usage.input_tokens, 1200)
        self.assertEqual(usage.output_tokens, 100)

    def test_failed_ai_operation_releases_reservation_without_charging(self):
        AICreditService.add_manual_credits(
            organization=self.organization,
            amount=20,
            reason="Test funding",
        )
        reservation = AICreditService.reserve_text(
            organization_id=self.organization.id,
            model="gpt-4.1-nano",
            instructions="Reply helpfully.",
            input_text="Hello",
            feature="engagement",
        )

        AICreditService.release(reservation)

        wallet = AICreditService.ensure_wallet(self.organization)
        wallet.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(wallet.balance, 20)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(
            reservation.status,
            AICreditReservation.Status.RELEASED,
        )
        self.assertFalse(
            wallet.transactions.filter(
                transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            ).exists()
        )

    def test_manual_debit_cannot_spend_reserved_credits(self):
        AICreditService.add_manual_credits(
            organization=self.organization,
            amount=10,
            reason="Test funding",
        )
        reservation = AICreditService.reserve_text(
            organization_id=self.organization.id,
            model="gpt-4.1-nano",
            instructions="Reply helpfully.",
            input_text="Hello",
            feature="engagement",
        )

        with self.assertRaises(AICreditError):
            AICreditService.deduct_manual_credits(
                organization=self.organization,
                amount=6,
                reason="Should fail",
            )

        AICreditService.release(reservation)

    def test_superadmin_block_prevents_new_ai_reservations(self):
        AICreditService.add_manual_credits(
            organization=self.organization,
            amount=100,
            reason="Test funding",
        )
        AICreditService.set_blocked(
            organization=self.organization,
            blocked=True,
        )

        with self.assertRaises(AICreditUnavailableError):
            AICreditService.reserve_text(
                organization_id=self.organization.id,
                model="gpt-4.1-nano",
                instructions="Reply helpfully.",
                input_text="Hello",
                feature="engagement",
            )


@patch.dict(
    os.environ,
    {
        "AI_CREDIT_MODEL_RATES_JSON": "",
        "AI_CREDIT_RESERVED_OUTPUT_TOKENS": "1000",
    },
)
@override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_AI_MODEL="gpt-4.1-nano",
)
class OpenAIProviderCreditGuardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Provider Credit Guard Organization",
        )
        self.client = Mock()
        self.client.responses.create.return_value = SimpleNamespace(
            output_text="Hello from SHVYA AI",
            model="gpt-4.1-nano",
            usage=SimpleNamespace(
                input_tokens=1200,
                output_tokens=100,
            ),
        )

    def _generate(self):
        return OpenAIProvider(client=self.client).generate_text(
            instructions="Reply helpfully.",
            input_text="Hello",
            metadata={
                "organization_id": str(self.organization.id),
                "lead_id": "lead-123",
                "task": "engagement",
            },
        )

    def test_zero_credit_organization_never_calls_openai(self):
        AICreditService.ensure_wallet(self.organization)

        with self.assertRaises(AIProviderPermanentError):
            self._generate()

        self.client.responses.create.assert_not_called()
        wallet = AICreditService.ensure_wallet(self.organization)
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(wallet.reserved_credits, 0)

    def test_funded_organization_calls_openai_and_settles_usage(self):
        AICreditService.add_manual_credits(
            organization=self.organization,
            amount=20,
            reason="Manual provider test allocation",
        )

        result = self._generate()

        self.assertEqual(result.text, "Hello from SHVYA AI")
        self.client.responses.create.assert_called_once()

        wallet = AICreditService.ensure_wallet(self.organization)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 14)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(wallet.lifetime_credits_used, 6)
        self.assertEqual(
            wallet.transactions.filter(
                transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            ).count(),
            1,
        )


@patch.dict(
    os.environ,
    {
        "AI_CREDIT_MODEL_RATES_JSON": "",
        "AI_CREDIT_RESERVED_OUTPUT_TOKENS": "1000",
    },
)
@override_settings(
    OPENAI_API_KEY="test-key",
    OPENAI_EMBEDDING_MODEL="text-embedding-3-small",
)
class EmbeddingCreditGuardTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Embedding Credit Guard Organization",
        )
        self.service = EmbeddingService(api_key="test-key")
        self.client = Mock()
        self.service._client = self.client
        self.client.embeddings.create.return_value = SimpleNamespace(
            data=[
                SimpleNamespace(
                    index=0,
                    embedding=[0.01] * EmbeddingService.DEFAULT_DIMENSIONS,
                )
            ],
            usage=SimpleNamespace(prompt_tokens=240),
        )

    def test_zero_credit_organization_never_calls_embedding_provider(self):
        AICreditService.ensure_wallet(self.organization)

        with self.assertRaises(EmbeddingError):
            self.service.embed_text(
                "What is the course fee?",
                organization_id=self.organization.id,
                feature="knowledge_retrieval",
            )

        self.client.embeddings.create.assert_not_called()
        wallet = AICreditService.ensure_wallet(self.organization)
        self.assertEqual(wallet.balance, 0)
        self.assertEqual(wallet.reserved_credits, 0)

    def test_funded_embedding_call_is_settled_to_same_wallet(self):
        AICreditService.add_manual_credits(
            organization=self.organization,
            amount=10,
            reason="Manual embedding test allocation",
        )

        vector = self.service.embed_text(
            "What is the course fee?",
            organization_id=self.organization.id,
            feature="knowledge_retrieval",
            reference_id="lead-embedding-test",
        )

        self.assertEqual(len(vector), EmbeddingService.DEFAULT_DIMENSIONS)
        self.client.embeddings.create.assert_called_once()

        wallet = AICreditService.ensure_wallet(self.organization)
        wallet.refresh_from_db()
        self.assertEqual(wallet.balance, 9)
        self.assertEqual(wallet.reserved_credits, 0)
        self.assertEqual(wallet.lifetime_credits_used, 1)

        usage = wallet.transactions.get(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
        )
        self.assertEqual(usage.amount, -1)
        self.assertEqual(usage.feature, "knowledge_retrieval")
        self.assertEqual(usage.input_tokens, 240)
        self.assertEqual(usage.output_tokens, 0)
