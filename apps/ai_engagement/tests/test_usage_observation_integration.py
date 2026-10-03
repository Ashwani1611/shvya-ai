"""Evaluation costs are the actual tenant-scoped reservation ledger, not wallet drift."""
import json
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings

from apps.ai_engagement.models import AICreditReservation, AICreditTransaction, OrgInfo
from apps.ai_engagement.services.credits import AICreditService
from apps.ai_engagement.services.usage_observation import observe_usage, usage_report
from apps.ai_engagement.services import recovery_evaluation as evaluation
from apps.ai_engagement.tests import test_recovery_validation_integration as fixtures
from apps.organizations.models import Organization


@override_settings(OPENAI_API_KEY="test-only-not-a-real-key")
class UsageObservationIntegrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Usage test org")
        self.other = Organization.objects.create(name="Other usage org")
        for org in (self.organization, self.other):
            AICreditService.add_manual_credits(organization=org, amount=1000, reason="Test funding")
        info, _ = OrgInfo.objects.get_or_create(organization=self.organization)
        info.ai_enabled, info.about = True, "Approved package details."
        info.save()

    def reserve(self, organization=None):
        return AICreditService.reserve_text(organization_id=(organization or self.organization).pk,
            model="test-model", instructions="Private fixture instructions", input_text="PRIVATE_QUERY",
            feature="engagement", reference_id="private-test-reference")

    def test_counts_only_captured_settled_usage(self):
        outside = self.reserve()
        AICreditService.settle(reservation=outside, input_tokens=2000, output_tokens=1000)
        with observe_usage(self.organization.pk) as usage:
            inside = self.reserve()
            AICreditService.settle(reservation=inside, input_tokens=1000, output_tokens=100)
            foreign = self.reserve(self.other)
            AICreditService.settle(reservation=foreign, input_tokens=2000, output_tokens=1000)
        inside.refresh_from_db()
        report = usage_report(usage)
        self.assertTrue(report["complete"])
        self.assertEqual(report["observed_reservations"], 1)
        self.assertEqual(report["settled_credits"], inside.actual_credits)
        self.assertEqual(report["input_tokens"], 1000)
        self.assertEqual(report["output_tokens"], 100)
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertIsNone(report["currency_cost"])

    def test_pending_usage_is_explicit_and_not_reported_as_final(self):
        with observe_usage(self.organization.pk) as usage:
            reserved = self.reserve()
        report = usage_report(usage)
        self.assertFalse(report["complete"])
        self.assertEqual(report["pending_reserved_credits"], reserved.reserved_credits)
        self.assertEqual(report["settled_credits"], 0)

    def test_released_failed_call_does_not_appear_as_a_charge(self):
        with observe_usage(self.organization.pk) as usage:
            reserved = self.reserve()
            AICreditService.release(reserved)
        report = usage_report(usage)
        self.assertTrue(report["complete"])
        self.assertEqual(report["settled_credits"], 0)
        self.assertEqual(report["released_reservations"], 1)

    def test_missing_reservation_is_not_zero_complete_cost(self):
        with observe_usage(self.organization.pk) as usage:
            reserved = self.reserve()
            AICreditService.release(reserved)
        AICreditReservation.objects.filter(pk=reserved.pk).delete()
        self.assertFalse(usage_report(usage)["complete"])

    def test_duplicate_ledger_rows_are_not_a_valid_cost_measurement(self):
        with observe_usage(self.organization.pk) as usage:
            reserved = self.reserve()
            AICreditService.settle(reservation=reserved, input_tokens=1000, output_tokens=100)
        row = AICreditTransaction.objects.get(reservation_id=reserved.pk)
        row.pk = None
        row.save()
        self.assertFalse(usage_report(usage)["complete"])

    def test_report_read_error_is_isolated_from_caller_transaction(self):
        def database_error(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 / 0")
        with observe_usage(self.organization.pk) as usage:
            self.reserve()
        with patch.object(AICreditReservation.objects, "filter", side_effect=database_error):
            report = usage_report(usage)
        self.assertEqual(report["status"], "unavailable")
        self.assertIsNone(report["settled_credits"])
        self.assertTrue(Organization.objects.filter(pk=self.organization.pk).exists())

    def test_paired_runner_reports_incremental_actual_credits(self):
        records, charged = [], []
        def charge():
            reservation = self.reserve()
            # Cross a real billing bucket; 100 and 1,000 tokens share one bucket.
            AICreditService.settle(reservation=reservation, input_tokens=1000,
                output_tokens=2000 if records[-1] else 100)
            reservation.refresh_from_db()
            charged.append(reservation.actual_credits)
        def runner():
            return fixtures.RecoveryValidationTests.fake_runner(self, records, callback=charge)
        with patch.object(evaluation, "_memory_playground", side_effect=runner):
            report = evaluation.evaluate(self.organization, fixtures.scenario())
        self.assertTrue(report["comparison_valid"])
        self.assertTrue(report["credit_usage_measured"])
        self.assertTrue(report["usage"]["incremental_complete"])
        self.assertEqual(records, [False, True])
        self.assertEqual(report["usage"]["baseline"]["settled_credits"], charged[0])
        self.assertEqual(report["usage"]["recovery"]["settled_credits"], charged[1])
        self.assertEqual(report["usage"]["incremental_credits"], charged[1] - charged[0])
        self.assertGreater(report["usage"]["incremental_credits"], 0)
        self.assertFalse(report["customer_messages_sent"])
        self.assertFalse(report["semantic_accuracy_verified"])

    def test_failed_runner_retains_charge_without_claiming_valid_comparison(self):
        records = []
        def fail():
            reserved = self.reserve()
            AICreditService.settle(reservation=reserved, input_tokens=1000, output_tokens=100)
            raise RuntimeError("PRIVATE_ERROR")
        with patch.object(evaluation, "_memory_playground", side_effect=lambda:
                fixtures.RecoveryValidationTests.fake_runner(self, records, callback=fail)):
            report = evaluation.evaluate(self.organization, fixtures.scenario())
        self.assertFalse(report["comparison_valid"])
        self.assertIsNone(report["usage"]["incremental_credits"])
        self.assertGreater(report["usage"]["baseline"]["settled_credits"], 0)
        self.assertNotIn("PRIVATE", json.dumps(report))

    def test_equal_billing_buckets_have_zero_incremental_credits(self):
        records = []
        def charge():
            reserved = self.reserve()
            AICreditService.settle(reservation=reserved, input_tokens=1000,
                output_tokens=1000 if records[-1] else 100)
        with patch.object(evaluation, "_memory_playground", side_effect=lambda:
                fixtures.RecoveryValidationTests.fake_runner(self, records, callback=charge)):
            report = evaluation.evaluate(self.organization, fixtures.scenario())
        self.assertEqual(records, [False, True])
        self.assertTrue(report["usage"]["incremental_complete"])
        self.assertEqual(report["usage"]["incremental_credits"], 0)
        self.assertEqual(report["usage"]["baseline"]["settled_credits"],
                         report["usage"]["recovery"]["settled_credits"])
