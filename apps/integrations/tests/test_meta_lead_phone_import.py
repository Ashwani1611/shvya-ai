import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import override_settings

from apps.crm.models import Pipeline, Stage
from apps.integrations.models import MetaLeadForm, MetaLeadPage
from apps.organizations.models import Organization

pytestmark = pytest.mark.django_db

WEBHOOK_PATH = "/webhooks/meta-leads/"
PAGE_ID = "meta-page-phone-tests"
FORM_ID = "meta-form-phone-tests"
APP_SECRET = "meta-page-secret"


def _body(payload):
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _signature(secret, body):
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _configured_meta_form():
    organization = Organization.objects.create(name="Meta Phone Import Org")
    pipeline = Pipeline.objects.get(organization=organization, name="Leads")
    stage = Stage.objects.filter(pipeline=pipeline).order_by("display_order").first()
    assert stage is not None

    page = MetaLeadPage(
        organization=organization,
        page_id=PAGE_ID,
        page_name="Meta Phone Test Page",
    )
    page.set_page_access_token("page-access-token")
    page.set_app_secret(APP_SECRET)
    page.save()

    MetaLeadForm.objects.create(
        page=page,
        form_id=FORM_ID,
        form_name="Meta Phone Test Form",
        pipeline=pipeline,
        stage=stage,
        field_mapping={
            "name": "full_name",
            "phone": "phone_number",
            "email": "email",
        },
    )
    return organization


def _webhook_payload(leadgen_id):
    return {
        "entry": [
            {
                "id": PAGE_ID,
                "changes": [
                    {
                        "field": "leadgen",
                        "value": {
                            "leadgen_id": leadgen_id,
                            "form_id": FORM_ID,
                        },
                    }
                ],
            }
        ]
    }


@override_settings(META_APP_SECRET="")
def test_meta_lead_without_real_phone_is_skipped_instead_of_using_leadgen_id(client):
    _configured_meta_form()
    leadgen_id = "123456789012345678"
    payload = _webhook_payload(leadgen_id)
    body = _body(payload)
    lead_data = {
        "form_id": FORM_ID,
        "field_data": [
            {"name": "full_name", "values": ["No Phone Lead"]},
            {"name": "email", "values": ["no-phone@example.com"]},
        ],
    }

    with (
        patch(
            "apps.integrations.views.meta_leads._fetch_lead",
            return_value=lead_data,
        ),
        patch("apps.integrations.views.meta_leads.upsert_lead") as upsert_lead,
    ):
        response = client.post(
            WEBHOOK_PATH,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_signature(APP_SECRET, body),
        )

    assert response.status_code == 200
    assert response.content == b"EVENT_RECEIVED"
    upsert_lead.assert_not_called()


@override_settings(META_APP_SECRET="")
def test_meta_lead_can_recover_a_real_phone_from_another_phone_like_field(client):
    organization = _configured_meta_form()
    leadgen_id = "987654321098765432"
    payload = _webhook_payload(leadgen_id)
    body = _body(payload)
    lead_data = {
        "form_id": FORM_ID,
        "field_data": [
            {"name": "full_name", "values": ["Recovered Phone Lead"]},
            {"name": "whatsapp_contact", "values": ["9876543210"]},
        ],
    }

    with (
        patch(
            "apps.integrations.views.meta_leads._fetch_lead",
            return_value=lead_data,
        ),
        patch(
            "apps.integrations.views.meta_leads.upsert_lead",
            return_value=(SimpleNamespace(id="lead-1"), True),
        ) as upsert_lead,
    ):
        response = client.post(
            WEBHOOK_PATH,
            data=body,
            content_type="application/json",
            HTTP_X_HUB_SIGNATURE_256=_signature(APP_SECRET, body),
        )

    assert response.status_code == 200
    upsert_lead.assert_called_once()
    call_kwargs = upsert_lead.call_args.kwargs
    assert call_kwargs["organization"] == organization
    assert call_kwargs["phone"] == "+919876543210"
    assert call_kwargs["phone"] != f"+{leadgen_id}"
    assert call_kwargs["attributes"]["meta_leadgen_id"] == leadgen_id
    assert "recovered" in call_kwargs["attributes"]["meta_import_warning"].lower()
