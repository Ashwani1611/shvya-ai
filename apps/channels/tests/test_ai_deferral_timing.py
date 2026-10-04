from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase
from django.utils import timezone

from services.channels.ai_send_gate import _finish, ai_send_gap_seconds
from services.channels.hosted_automation_service import AI_RESPONSE_DELAY_SECONDS


class AIDeferralTimingTests(SimpleTestCase):
    def test_hosted_initial_wait_and_send_gap_are_45_seconds(self):
        self.assertEqual(AI_RESPONSE_DELAY_SECONDS, 45)
        self.assertEqual(ai_send_gap_seconds(SimpleNamespace(connection_type="hosted")), 45)

    def test_deferral_releases_claim_without_extending_previous_send_gap(self):
        now = timezone.now()
        message = SimpleNamespace(account_id="account", account=SimpleNamespace(connection_type="hosted"),
                                  status="queued")
        for previous in (None, now - timedelta(seconds=44)):
            with self.subTest(previous=previous), patch(
                "services.channels.ai_send_gate.AIMessageSendState.objects.filter"
            ) as rows:
                rows.return_value.first.return_value = SimpleNamespace(last_sent_at=previous)
                _finish(message, "token", deferred=True)
                updates = rows.return_value.update.call_args.kwargs
                self.assertIsNone(updates["claimed_until"])
                self.assertEqual(updates["next_send_at"],
                                 previous + timedelta(seconds=45) if previous else None)

    def test_successful_hosted_send_opens_slot_at_sent_time_plus_45(self):
        now = timezone.now()
        message = SimpleNamespace(account_id="account", account=SimpleNamespace(connection_type="hosted"),
                                  status="sent", sent_at=now)
        with patch("services.channels.ai_send_gate.AIMessageSendState.objects.filter") as rows:
            _finish(message, "token")
            self.assertEqual(rows.return_value.update.call_args.kwargs["next_send_at"],
                             now + timedelta(seconds=45))
