import pytest
from django.core.mail import EmailMultiAlternatives


@pytest.fixture(autouse=True)
def smart_trigger_email_idempotency_test_mailbox(request, monkeypatch):
    """Keep the durable-claim test focused on duplicate prevention.

    Workflow email delivery now correctly requires an organization's connected
    Connect Hub mailbox. The legacy SmartTrigger idempotency test predates that
    transport rule and only needs a deterministic mail sink so it can verify
    that calling ``deliver_email`` twice sends once. Connected-mailbox behavior
    is covered separately in ``test_connected_email_delivery.py``.
    """
    if request.node.nodeid != (
        "apps/triggers/tests/test_smart_triggers.py::SmartTriggerTests::"
        "test_email_has_durable_claim_and_no_duplicate"
    ):
        return

    def send_test_email(
        *,
        organization,
        to,
        subject,
        text_body,
        html_body=None,
        reply_to=None,
        headers=None,
    ):
        del organization
        message = EmailMultiAlternatives(
            subject=str(subject or ""),
            body=str(text_body or ""),
            to=[to] if isinstance(to, str) else list(to or []),
            reply_to=[reply_to] if isinstance(reply_to, str) else reply_to,
            headers=headers or None,
        )
        if html_body:
            message.attach_alternative(html_body, "text/html")
        return message.send(fail_silently=False)

    monkeypatch.setattr(
        "services.triggers.actions.send_organization_email",
        send_test_email,
    )
