import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from django.test import RequestFactory, override_settings

from apps.integrations.models import MetaLeadPage
from apps.integrations.views import meta_leads
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

WEBHOOK_PATH = "/webhooks/meta-leads/"


def _payload_bytes(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _sha256_signature(secret, body):
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _page_with_secret(page_id="page-123", secret="page-secret"):
    organization = Organization.objects.create(name=f"Meta Webhook Org {page_id}")
    page = MetaLeadPage(
        organization=organization,
        page_id=page_id,
        page_name="Lead Ads Page",
    )
    page.set_page_access_token("page-access-token")
    page.set_app_secret(secret)
    page.save()
    return page


@override_settings(META_LEAD_VERIFY_TOKEN="verify-token")
def test_get_verification_handshake_is_unchanged(client):
    response = client.get(
        WEBHOOK_PATH,
        {
            "hub.verify_token": "verify-token",
            "hub.challenge": "challenge-123",
        },
    )

    assert response.status_code == 200
    assert response.content == b"challenge-123"


@override_settings(META_APP_SECRET="")
def test_post_is_rejected_when_no_app_secret_is_configured(client):
    body = _payload_bytes({"entry": []})

    response = client.post(WEBHOOK_PATH, data=body, content_type="application/json")

    assert response.status_code == 403
    assert response.content == b"Webhook signature verification unavailable"


@override_settings(META_APP_SECRET="global-secret")
def test_post_is_rejected_when_signature_header_is_missing(client):
    body = _payload_bytes({"entry": []})

    response = client.post(WEBHOOK_PATH, data=body, content_type="application/json")

    assert response.status_code == 403
    assert response.content == b"Invalid webhook signature"


@override_settings(META_APP_SECRET="global-secret")
def test_post_is_rejected_when_signature_is_invalid(client):
    body = _payload_bytes({"entry": []})

    response = client.post(
        WEBHOOK_PATH,
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256="sha256=not-valid",
    )

    assert response.status_code == 403
    assert response.content == b"Invalid webhook signature"


@override_settings(META_APP_SECRET="global-secret")
def test_valid_global_sha256_signature_is_accepted(client):
    body = _payload_bytes({"entry": []})

    response = client.post(
        WEBHOOK_PATH,
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=_sha256_signature("global-secret", body),
    )

    assert response.status_code == 200
    assert response.content == b"EVENT_RECEIVED"


@override_settings(META_APP_SECRET="")
def test_valid_page_specific_sha256_signature_is_accepted(client):
    _page_with_secret(page_id="page-456", secret="page-specific-secret")
    body = _payload_bytes({"entry": [{"id": "page-456", "changes": []}]})

    response = client.post(
        WEBHOOK_PATH,
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=_sha256_signature("page-specific-secret", body),
    )

    assert response.status_code == 200
    assert response.content == b"EVENT_RECEIVED"


@override_settings(META_APP_SECRET="")
def test_invalid_signature_cannot_reach_meta_lead_fetch(client):
    _page_with_secret(page_id="page-789", secret="page-secret")
    body = _payload_bytes(
        {
            "entry": [
                {
                    "id": "page-789",
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {"leadgen_id": "1234567890"},
                        }
                    ],
                }
            ]
        }
    )

    with patch("apps.integrations.views.meta_leads._fetch_lead") as fetch_lead:
        response = client.post(
            WEBHOOK_PATH,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256="sha256=invalid",
        )

    assert response.status_code == 403
    fetch_lead.assert_not_called()


def test_replacement_signature_helper_fails_closed_without_secret_or_header():
    request = RequestFactory().post(
        WEBHOOK_PATH,
        data=b"{}",
        content_type="application/json",
    )

    assert meta_leads._signature_is_valid(request, "") is False
    assert meta_leads._signature_is_valid(request, "configured-secret") is False


@override_settings(META_APP_SECRET="global-secret")
def test_malformed_json_keeps_existing_bad_request_contract(client):
    response = client.post(
        WEBHOOK_PATH,
        data=b"{not-json",
        content_type="application/json",
    )

    assert response.status_code == 400
