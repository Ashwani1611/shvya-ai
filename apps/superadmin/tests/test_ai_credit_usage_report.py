from datetime import datetime

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.ai_engagement.models import AICreditTransaction, AICreditWallet
from apps.organizations.models import Organization
from apps.superadmin.ai_credit_views import _build_usage_report


class SuperadminAICreditUsageReportTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            email="superadmin-ai-report@example.com",
            password="test-password-123",
            name="Super Admin",
        )
        session = SessionStore()
        set_authenticated_user(session, self.superuser)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

        self.organization = Organization.objects.create(name="ABC Pvt Ltd")
        self.wallet = AICreditWallet.objects.create(
            organization=self.organization,
            balance=50000,
        )

    def _transaction(self, *, transaction_type, amount, feature="", description=""):
        return AICreditTransaction.objects.create(
            organization=self.organization,
            wallet=self.wallet,
            transaction_type=transaction_type,
            amount=amount,
            balance_after=50000,
            feature=feature,
            description=description,
        )

    def _wallet_url(self):
        return reverse(
            "superadmin-organization-ai-credits",
            kwargs={"organization_id": self.organization.id},
        )

    def test_report_groups_usage_by_action_as_coins_but_keeps_raw_totals(self):
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.MANUAL_CREDIT,
            amount=300,
            description="Initial manual allocation",
        )
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-18,
            feature="engagement",
        )
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-12,
            feature="qualification",
        )
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-7,
            feature="internal_summary",
        )
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-4,
            feature="knowledge_embedding",
        )

        report = _build_usage_report(self.wallet.transactions.all())

        self.assertEqual(report["credits_added"], 300)
        self.assertEqual(report["credits_added_display"], "10.00")
        self.assertEqual(report["credits_added_credits_display"], "300")
        self.assertEqual(report["ai_used"], 41)
        self.assertEqual(report["ai_used_display"], "1.37")
        self.assertEqual(report["net_change"], 259)
        self.assertEqual(
            [(row["label"], row["signed_display"]) for row in report["usage_rows"]],
            [
                ("AI Engagement", "-0.60"),
                ("Qualification", "-0.40"),
                ("Conversation Summary", "-0.23"),
                ("Knowledge Embedding", "-0.13"),
            ],
        )

    def test_custom_date_filter_shows_coin_summary_and_raw_credit_history(self):
        inside = self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-18,
            feature="engagement",
            description="Inside selected range",
        )
        outside = self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-12,
            feature="qualification",
            description="Outside selected range",
        )

        current_tz = timezone.get_current_timezone()
        inside_at = timezone.make_aware(datetime(2026, 9, 3, 12, 0), current_tz)
        outside_at = timezone.make_aware(datetime(2026, 8, 20, 12, 0), current_tz)
        AICreditTransaction.objects.filter(pk=inside.pk).update(created_at=inside_at)
        AICreditTransaction.objects.filter(pk=outside.pk).update(created_at=outside_at)

        response = self.client.get(
            f"{self._wallet_url()}?from=2026-09-01&to=2026-09-05"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "AI Engagement")
        self.assertContains(response, "-0.60")
        # Transaction history remains exact internal credits.
        self.assertContains(response, "-18")
        self.assertContains(response, "Inside selected range")
        self.assertNotContains(response, "Qualification")
        self.assertNotContains(response, "Outside selected range")
        self.assertContains(response, "01 Sep 2026")
        self.assertContains(response, "05 Sep 2026")

    def test_manual_deductions_are_separate_from_ai_usage(self):
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.MANUAL_CREDIT,
            amount=300,
        )
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.MANUAL_DEBIT,
            amount=-90,
        )
        self._transaction(
            transaction_type=AICreditTransaction.TransactionType.AI_USAGE,
            amount=-30,
            feature="engagement",
        )

        report = _build_usage_report(self.wallet.transactions.all())

        self.assertEqual(report["credits_added"], 300)
        self.assertEqual(report["credits_added_display"], "10.00")
        self.assertEqual(report["manual_deducted"], 90)
        self.assertEqual(report["manual_deducted_display"], "3.00")
        self.assertEqual(report["ai_used"], 30)
        self.assertEqual(report["ai_used_display"], "1.00")
        self.assertEqual(report["net_change"], 180)
        self.assertEqual(report["net_change_display"], "+6.00")

    def test_superadmin_adds_whole_coins_as_30_internal_credits_each(self):
        response = self.client.post(
            self._wallet_url(),
            {
                "action": "add",
                "amount": "2",
                "reason": "Two coin recharge",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 50060)
        transaction = self.wallet.transactions.latest("created_at")
        self.assertEqual(transaction.amount, 60)
        self.assertEqual(transaction.balance_after, 50060)

    def test_superadmin_deducts_coins_in_30_credit_units(self):
        response = self.client.post(
            self._wallet_url(),
            {
                "action": "deduct",
                "amount": "1",
                "reason": "One coin correction",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 49970)
        transaction = self.wallet.transactions.latest("created_at")
        self.assertEqual(transaction.amount, -30)

    def test_low_balance_threshold_is_entered_in_coins(self):
        response = self.client.post(
            self._wallet_url(),
            {"action": "threshold", "threshold": "3"},
        )

        self.assertEqual(response.status_code, 302)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.low_credit_threshold, 90)
