from unittest.mock import patch

from django.db import DatabaseError
from django.test import TestCase

from apps.ai_engagement.models import AICreditReservation
from apps.ai_engagement.services.credits import AICreditService
from apps.ai_engagement.services.usage_attribution import capture_usage, comparison_cost, usage_report
from apps.organizations.models import Organization


class UsageAttributionTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Metered comparison")
        self.other = Organization.objects.create(name="Other metered tenant")
        for org in (self.org, self.other):
            AICreditService.add_manual_credits(organization=org, amount=100, reason="Test credits")

    def reserve(self, org, credits=5):
        return AICreditService._reserve(organization_id=org.pk, model="test", feature="test",
            reference_id="private-reference", credits=credits, estimated_input_tokens=100, estimated_output_tokens=20)

    def test_only_context_owned_reservations_count_not_wallet_changes(self):
        with capture_usage(self.org.pk) as scope:
            mine = self.reserve(self.org)
            self.reserve(self.other)
            AICreditService.settle(reservation=mine, input_tokens=100, output_tokens=20, charge_override=3)
        unrelated = self.reserve(self.org)
        AICreditService.settle(reservation=unrelated, input_tokens=100, output_tokens=20, charge_override=9)
        report = usage_report(scope)
        self.assertEqual(report["total_credits"], 3)
        self.assertEqual(report["reservation_count"], 1)
        self.assertNotIn("private-reference", str(report))

    def test_incremental_cost_compares_exact_settled_variants(self):
        with capture_usage(self.org.pk) as baseline:
            a = self.reserve(self.org)
            AICreditService.settle(reservation=a, input_tokens=100, output_tokens=20, charge_override=2)
        with capture_usage(self.org.pk) as recovery:
            b = self.reserve(self.org)
            AICreditService.settle(reservation=b, input_tokens=100, output_tokens=20, charge_override=4)
        report = comparison_cost(baseline, recovery)
        self.assertEqual(report["incremental_credits"], 2)
        self.assertTrue(report["accounting_complete"])

    def test_active_reservation_does_not_become_zero_cost(self):
        with capture_usage(self.org.pk) as scope:
            self.reserve(self.org)
        report = usage_report(scope)
        self.assertFalse(report["accounting_complete"])
        self.assertIsNone(report["total_credits"])

    def test_reporting_storage_error_does_not_break_caller_transaction(self):
        with capture_usage(self.org.pk) as scope:
            self.reserve(self.org)
        with patch.object(AICreditReservation.objects, "filter", side_effect=DatabaseError("storage")):
            report = usage_report(scope)
        self.assertFalse(report["accounting_complete"])
        self.assertTrue(Organization.objects.filter(pk=self.org.pk).exists())
