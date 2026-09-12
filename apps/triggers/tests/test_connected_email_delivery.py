from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from services.triggers.actions import _apply, deliver_email


class WorkflowConnectedEmailTests(SimpleTestCase):
    def _lead(self):
        organization = SimpleNamespace(name="Acme")
        lead = SimpleNamespace(
            name="Jane Doe",
            phone="",
            email="jane@example.com",
            lead_source="",
            organization=organization,
            pipeline_id=None,
            stage_id=None,
            attributes={},
        )
        return organization, lead

    @override_settings(FOLLOWUP_EMAIL_DELIVERY_ENABLED=False)
    def test_email_action_does_not_use_legacy_global_flag(self):
        _, lead = self._lead()
        run = SimpleNamespace(
            action_type="email",
            action={"subject": "Hello", "body": "Welcome"},
            rule=SimpleNamespace(created_by=SimpleNamespace(name="Owner", email="owner@example.com")),
            status=None,
            detail="",
        )

        _apply(run, lead)

        self.assertEqual(run.status, "email_ready")

    @patch("services.triggers.actions.send_organization_email")
    @patch("services.triggers.actions.TriggerRun.objects")
    def test_delivery_uses_connected_organization_mailbox(self, objects, send_email):
        organization, lead = self._lead()
        run = SimpleNamespace(
            id="run-1",
            action={"subject": "Hello {{lead_name}}", "body": "Welcome to {{org_name}}"},
            rule=SimpleNamespace(
                enabled=True,
                created_by=SimpleNamespace(name="Owner", email="owner@example.com"),
            ),
            lead=lead,
            status="email_ready",
            detail="",
            finished_at=None,
            save=Mock(),
        )
        objects.filter.return_value.update.return_value = 1
        objects.select_related.return_value.get.return_value = run

        deliver_email("run-1")

        send_email.assert_called_once_with(
            organization=organization,
            to="jane@example.com",
            subject="Hello Jane Doe",
            text_body="Welcome to Acme",
            headers={"Message-ID": "<smart-trigger-run-1@shvya-ai.com>"},
        )
        self.assertEqual(run.status, "completed")
        run.save.assert_called_once()
