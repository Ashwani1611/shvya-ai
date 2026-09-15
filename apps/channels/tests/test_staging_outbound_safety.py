from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.channels.providers.whatsapp import WhatsAppAPIError, WhatsAppClient
from services.channels import hosted_whatsapp_transport, instagram_service
from services.channels.staging_outbound_safety import staging_outbound_allowed
from services.channels.whatsapp_service import WhatsAppSendError


@override_settings(
    APP_ENV="staging",
    OUTBOUND_MESSAGING_ENABLED=False,
    STAGING_ALLOWED_RECIPIENTS=set(),
)
def test_staging_outbound_is_blocked_by_default():
    assert staging_outbound_allowed("+919999999999") is False


@override_settings(
    APP_ENV="staging",
    OUTBOUND_MESSAGING_ENABLED=True,
    STAGING_ALLOWED_RECIPIENTS={"+919999999999"},
)
def test_staging_outbound_only_allows_explicit_test_recipient():
    assert staging_outbound_allowed("919999999999") is True
    assert staging_outbound_allowed("918888888888") is False


@override_settings(
    APP_ENV="production",
    OUTBOUND_MESSAGING_ENABLED=False,
    STAGING_ALLOWED_RECIPIENTS=set(),
)
def test_production_behavior_is_not_blocked_by_staging_guard():
    assert staging_outbound_allowed("+919999999999") is True


@override_settings(
    APP_ENV="staging",
    OUTBOUND_MESSAGING_ENABLED=False,
    STAGING_ALLOWED_RECIPIENTS=set(),
)
def test_meta_whatsapp_transport_is_blocked_before_network_call():
    client = WhatsAppClient(phone_number_id="123", access_token="test")
    with patch.object(client, "_post") as post:
        with pytest.raises(WhatsAppAPIError, match="Staging outbound messaging blocked"):
            client.send_text_message(to="+919999999999", body="do not send")
    post.assert_not_called()


@override_settings(
    APP_ENV="staging",
    OUTBOUND_MESSAGING_ENABLED=False,
    STAGING_ALLOWED_RECIPIENTS=set(),
)
def test_hosted_whatsapp_transport_is_blocked_before_gateway_call():
    message = SimpleNamespace(to_number="+919999999999")
    with pytest.raises(WhatsAppSendError, match="non-allowlisted"):
        hosted_whatsapp_transport.send_hosted_message(message=message)


@override_settings(
    APP_ENV="staging",
    OUTBOUND_MESSAGING_ENABLED=False,
    STAGING_ALLOWED_RECIPIENTS=set(),
)
def test_instagram_transport_is_blocked_before_meta_call():
    message = SimpleNamespace(recipient_id="17841400000000000")
    with pytest.raises(instagram_service.InstagramAPIError, match="non-allowlisted"):
        instagram_service.send_queued_message(message)
