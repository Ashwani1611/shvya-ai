from datetime import timedelta
from types import SimpleNamespace

from django.test import SimpleTestCase
from django.utils import timezone

from services.copilot_service import (
    DEFAULT_CONFIG,
    can_snooze,
    evaluate_lead,
)


class CopilotSignalEvaluationTests(SimpleTestCase):
    def make_lead(
        self,
        *,
        created_at=None,
        stage_entered_at=None,
        attributes=None,
        messages=None,
        calls=None,
        phone="919999999999",
    ):
        now = timezone.now()
        return SimpleNamespace(
            id="lead-1",
            pipeline_id="pipeline-1",
            created_at=created_at or now,
            stage_entered_at=stage_entered_at or now,
            attributes=attributes or {},
            phone=phone,
            stage=SimpleNamespace(name="New Lead"),
            _copilot_messages=messages or [],
            _copilot_calls=calls or [],
        )

    def message(
        self,
        *,
        direction,
        created_at,
        body="hello",
        status="received",
        raw_payload=None,
    ):
        return SimpleNamespace(
            direction=direction,
            created_at=created_at,
            body=body,
            status=status,
            raw_payload=raw_payload or {},
        )

    def call(self, *, called_at, status="completed"):
        return SimpleNamespace(
            called_at=called_at,
            created_at=called_at,
            status=status,
        )

    def config(self, **overrides):
        result = dict(DEFAULT_CONFIG)
        result.update(overrides)
        return result

    def test_reply_pending_ignores_automated_outbound_reply(self):
        now = timezone.now()
        messages = [
            self.message(
                direction="outbound",
                created_at=now - timedelta(hours=4),
                status="sent",
                raw_payload={"shvya_ai": {"source": "ai_reply"}},
            ),
            self.message(
                direction="inbound",
                created_at=now - timedelta(hours=3),
                body="I am interested",
            ),
        ]
        lead = self.make_lead(
            created_at=now - timedelta(days=1),
            messages=messages,
        )

        signals = evaluate_lead(
            lead,
            config=self.config(),
            pipeline_uses_api=False,
            now=now,
        )

        reply_pending = next(item for item in signals if item["flag_code"] == "R1")
        self.assertEqual(reply_pending["severity"], "medium")
        self.assertNotIn("R2", {item["flag_code"] for item in signals})

    def test_new_lead_no_contact_becomes_critical_after_24_hours(self):
        now = timezone.now()
        lead = self.make_lead(
            created_at=now - timedelta(hours=25),
            stage_entered_at=now,
        )

        signals = evaluate_lead(
            lead,
            config=self.config(),
            pipeline_uses_api=False,
            now=now,
        )

        signal = next(item for item in signals if item["flag_code"] == "R2")
        self.assertEqual(signal["severity"], "critical")

    def test_no_automation_signal_suppresses_other_flags(self):
        now = timezone.now()
        lead = self.make_lead(
            created_at=now - timedelta(days=3),
            stage_entered_at=now,
            attributes={
                "active_sequence_id": None,
                "upcoming_send_at": None,
            },
        )

        signals = evaluate_lead(
            lead,
            config=self.config(),
            pipeline_uses_api=False,
            now=now,
        )

        self.assertEqual([item["flag_code"] for item in signals], ["H3"])
        self.assertEqual(signals[0]["severity"], "high")

    def test_h2_suppresses_call_gap(self):
        now = timezone.now()
        messages = [
            self.message(
                direction="inbound",
                created_at=now - timedelta(days=4),
            )
        ]
        calls = [
            self.call(
                called_at=now - timedelta(days=3, hours=2),
                status="completed",
            )
        ]
        lead = self.make_lead(
            created_at=now - timedelta(days=8),
            stage_entered_at=now,
            messages=messages,
            calls=calls,
        )

        signals = evaluate_lead(
            lead,
            config=self.config(),
            pipeline_uses_api=False,
            now=now,
        )
        codes = {item["flag_code"] for item in signals}

        self.assertIn("H2", codes)
        self.assertNotIn("C2", codes)

    def test_cloud_api_delivery_failure_escalates_after_three_failures(self):
        now = timezone.now()
        messages = [
            self.message(
                direction="outbound",
                created_at=now - timedelta(minutes=30),
                status="failed",
            ),
            self.message(
                direction="outbound",
                created_at=now - timedelta(minutes=20),
                status="failed",
            ),
            self.message(
                direction="outbound",
                created_at=now - timedelta(minutes=10),
                status="failed",
            ),
        ]
        lead = self.make_lead(messages=messages)

        signals = evaluate_lead(
            lead,
            config=self.config(copilot_call_flags_enabled=False),
            pipeline_uses_api=True,
            now=now,
        )
        delivery = next(item for item in signals if item["flag_code"] == "R3")

        self.assertEqual(delivery["severity"], "high")
        self.assertEqual(delivery["metadata"]["failed_count"], 3)

    def test_critical_silent_and_dormant_flags_cannot_be_snoozed(self):
        self.assertFalse(can_snooze("R3", "medium"))
        self.assertFalse(can_snooze("H2", "critical"))
        self.assertFalse(can_snooze("X4", "critical"))
        self.assertTrue(can_snooze("H2", "high"))
        self.assertTrue(can_snooze("X4", "high"))
