from django.test import TestCase

from apps.ai_engagement.models import (
    AICreditTransaction,
    AICreditWallet,
    KnowledgeSource,
)
from apps.channels.models import WhatsAppAccount
from apps.followups.models import FollowupSequence
from apps.organizations.models import Organization
from apps.superadmin.templatetags.superadmin_metrics import (
    build_organization_metrics,
)


class SuperadminOrganizationMetricsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Metrics Org")
        self.wallet = AICreditWallet.objects.create(
            organization=self.organization,
            balance=80,
            reserved_credits=5,
            lifetime_credits_added=100,
            lifetime_credits_used=20,
        )

    def _usage(self, *, feature, task, amount=-1):
        return AICreditTransaction.objects.create(
            organization=self.organization,
            wallet=self.wallet,
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=amount,
            balance_after=80,
            feature=feature,
            metadata={"task": task},
        )

    def test_builds_real_ai_credit_and_usage_metrics(self):
        self._usage(feature="engagement", task="engagement")
        self._usage(feature="engagement", task="bump_up")
        self._usage(feature="qualification", task="lead_qualification_summary")

        KnowledgeSource.objects.create(
            organization=self.organization,
            source_type=KnowledgeSource.SourceType.URL,
            name="Knowledge",
            url="https://example.com/knowledge",
            is_active=True,
        )

        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Metrics WhatsApp",
        )
        FollowupSequence.objects.create(
            organization=self.organization,
            name="Default Follow-up",
            whatsapp_account=account,
        )

        metrics = build_organization_metrics([self.organization])[
            str(self.organization.id)
        ]

        self.assertEqual(metrics["ai_messages"], 1)
        self.assertEqual(metrics["ai_qualifications"], 1)
        self.assertEqual(metrics["ai_bumpups"], 1)
        self.assertEqual(metrics["afs_sent"], 0)
        self.assertEqual(metrics["credits_used"], 20)
        self.assertEqual(metrics["credits_remaining"], 75)
        self.assertEqual(metrics["credits_total"], 100)
        self.assertTrue(metrics["kb_setup"])
        self.assertEqual(metrics["sequences"], 1)

    def test_organization_without_wallet_returns_zero_credit_metrics(self):
        organization = Organization.objects.create(name="No Wallet Org")

        metrics = build_organization_metrics([organization])[str(organization.id)]

        self.assertEqual(metrics["credits_used"], 0)
        self.assertEqual(metrics["credits_remaining"], 0)
        self.assertEqual(metrics["credits_total"], 0)
        self.assertEqual(metrics["ai_messages"], 0)
        self.assertFalse(metrics["kb_setup"])
