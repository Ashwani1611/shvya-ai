from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from apps.followups.models import FollowupExecution
from apps.integrations.services.email import EmailConfigurationError
from services.followup_service import _send_email_step


class ConnectedEmailFollowupTests(SimpleTestCase):
    def _objects(self):
        organization = SimpleNamespace(name="Acme")
        creator = SimpleNamespace(name="Owner", email="owner@example.com")
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
        state = SimpleNamespace(
            lead=lead,
            organization=organization,
            sequence=SimpleNamespace(created_by=creator),
            last_sent_at=None,
            save=Mock(),
        )
        step = SimpleNamespace(
            email_subject="Hi {{lead_first_name}}",
            email_body="Welcome to {{org_name}}",
        )
        execution = SimpleNamespace(
            status=None,
            finished_at=None,
            payload=None,
            save=Mock(),
        )
        return organization, state, step, execution

    @patch("services.followup_service._repeat_or_advance")
    @patch("services.followup_service.send_organization_email")
    def test_email_step_uses_connected_organization_mailbox(
        self,
        send_email,
        repeat_or_advance,
    ):
        organization, state, step, execution = self._objects()

        _send_email_step(state, step, execution)

        send_email.assert_called_once_with(
            organization=organization,
            to="jane@example.com",
            subject="Hi Jane",
            text_body="Welcome to Acme",
        )
        self.assertEqual(execution.status, FollowupExecution.Status.SENT)
        self.assertEqual(
            execution.payload,
            {"recipient": "jane@example.com", "subject": "Hi Jane"},
        )
        execution.save.assert_called_once()
        state.save.assert_called_once()
        repeat_or_advance.assert_called_once_with(
            state,
            step,
            completed_at=execution.finished_at,
        )

    @patch(
        "services.followup_service.send_organization_email",
        side_effect=EmailConfigurationError("No connected email account."),
    )
    def test_missing_connected_mailbox_is_not_reported_as_dns_gate(self, send_email):
        _, state, step, execution = self._objects()

        with self.assertRaisesMessage(
            EmailConfigurationError,
            "No connected email account.",
        ):
            _send_email_step(state, step, execution)

        send_email.assert_called_once()
        execution.save.assert_not_called()
