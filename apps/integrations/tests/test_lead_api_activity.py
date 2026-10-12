"""Lead activity evidence must describe CRM outcomes, not just HTTP status."""

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
import requests
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.core.models import APIActivity
from apps.crm.models import Lead, Pipeline, Stage
from apps.integrations.models import (
    IndiaMartConnection,
    IndiaMartReceipt,
    JustDialIntegration,
    MetaLeadForm,
    MetaLeadPage,
)
from apps.organizations.models import Organization


pytestmark = pytest.mark.django_db


@pytest.fixture
def destination():
    organization = Organization.objects.create(name="Lead activity test")
    pipeline = Pipeline.objects.create(organization=organization, name="Activity pipeline")
    stage = Stage.objects.create(pipeline=pipeline, name="Enquiries", display_order=0)
    return organization, pipeline, stage


def _activities(integration):
    return APIActivity.objects.filter(integration=integration, kind="lead_ingestion")


def _assert_private(activity):
    serialized = json.dumps({
        "summary": activity.summary,
        "error_message": activity.error_message,
        "metadata": activity.metadata,
        "provider_reference": activity.provider_reference,
        "endpoint": activity.endpoint,
    })
    for private in ("9876543210", "buyer@example.test", "Private Buyer", "private-token"):
        assert private not in serialized


def test_justdial_created_duplicate_and_updated_are_distinct(client, destination):
    organization, pipeline, stage = destination
    connection = JustDialIntegration.objects.create(
        organization=organization, pipeline=pipeline, stage=stage, is_enabled=True,
    )
    connection.generate_webhook_token()
    connection.save()
    url = reverse("justdial-webhook", kwargs={"token": connection.webhook_token})
    payload = {
        "leadid": "JD-activity-1", "name": "Private Buyer", "mobile": "9876543210",
        "email": "buyer@example.test", "token": "private-token",
    }
    assert client.post(url, payload, content_type="application/json").status_code == 200
    assert client.post(url, payload, content_type="application/json").status_code == 200
    assert client.post(url, {**payload, "leadid": "JD-activity-2"}, content_type="application/json").status_code == 200

    activities = list(_activities("justdial").order_by("created_at"))
    assert [item.status for item in activities] == ["completed", "skipped", "completed"]
    assert [item.metadata["result"] for item in activities] == ["created", "duplicate", "updated"]
    assert activities[1].error_code == "duplicate_lead_id"
    lead = Lead.objects.get(organization=organization)
    for activity in activities:
        assert activity.organization_id == organization.pk
        assert activity.connection_id == str(connection.pk)
        assert activity.entity_id == str(lead.pk)
        _assert_private(activity)


def test_justdial_paused_and_invalid_request_are_visible(client, destination):
    organization, pipeline, stage = destination
    connection = JustDialIntegration.objects.create(
        organization=organization, pipeline=pipeline, stage=stage, is_enabled=False,
    )
    connection.generate_webhook_token()
    connection.save()
    url = reverse("justdial-webhook", kwargs={"token": connection.webhook_token})
    response = client.post(url, {"leadid": "JD-paused", "mobile": "9876543210"}, content_type="application/json")
    assert response.status_code == 503
    activity = _activities("justdial").get()
    assert activity.status == "blocked"
    assert activity.error_code == "intake_disabled"
    assert activity.organization_id == organization.pk

    response = client.post(url, "{", content_type="application/json")
    assert response.status_code == 400
    activity = _activities("justdial").get(error_code="invalid_payload")
    assert activity.status == "failed"
    assert activity.organization_id == organization.pk


@pytest.fixture
def indiamart(destination):
    organization, pipeline, stage = destination
    connection = IndiaMartConnection.objects.create(
        organization=organization, pipeline=pipeline, stage=stage,
        is_enabled=True, generated_at=timezone.now(),
    )
    return connection, reverse("indiamart-ingest", kwargs={"token": connection.webhook_token})


INDIAMART_ROW = {
    "UNIQUE_QUERY_ID": "IM-activity-1", "SENDER_NAME": "Private Buyer",
    "SENDER_MOBILE": "9876543210", "SENDER_EMAIL": "buyer@example.test",
    "SENDER_COUNTRY_ISO": "IN", "QUERY_MESSAGE": "private-token",
}


def test_indiamart_success_and_duplicate_have_durable_outcomes(client, indiamart):
    connection, url = indiamart
    with patch("apps.integrations.views.indiamart._schedule_new_lead_welcome"):
        for _ in range(2):
            assert client.post(url, INDIAMART_ROW, content_type="application/json").status_code == 200
    activities = list(_activities("indiamart").order_by("created_at"))
    assert [item.metadata["result"] for item in activities] == ["created", "duplicate"]
    assert [item.status for item in activities] == ["completed", "skipped"]
    for activity in activities:
        assert activity.organization_id == connection.organization_id
        assert activity.provider_reference == "IM-activity-1"
        assert activity.entity_id == str(Lead.objects.get(organization=connection.organization).pk)
        _assert_private(activity)
    assert IndiaMartReceipt.objects.filter(connection=connection).count() == 1


def test_indiamart_batch_rollback_leaves_failure_without_false_success(client, indiamart):
    connection, url = indiamart
    response = client.post(url, {
        "RESPONSE": [INDIAMART_ROW, {"UNIQUE_QUERY_ID": "IM-invalid"}],
    }, content_type="application/json")
    assert response.status_code == 400
    assert not Lead.objects.filter(organization=connection.organization).exists()
    assert not IndiaMartReceipt.objects.filter(connection=connection).exists()
    activity = _activities("indiamart").get()
    assert activity.status == "failed"
    assert activity.operation == "lead.batch_import"
    assert activity.provider_reference == "IM-invalid"
    assert activity.error_code == "invalid_phone"
    assert activity.metadata["rolled_back"] is True
    assert activity.metadata["batch_size"] == 2
    assert activity.organization_id == connection.organization_id
    _assert_private(activity)


@pytest.fixture
def meta_page(destination):
    organization, pipeline, stage = destination
    page = MetaLeadPage(organization=organization, page_id="page-activity")
    page.set_page_access_token("private-token")
    page.set_app_secret("signature-secret")
    page.save()
    MetaLeadForm.objects.create(
        page=page, form_id="form-activity", form_name="Activity form",
        pipeline=pipeline, stage=stage,
        field_mapping={"name": "full_name", "phone": "phone_number", "email": "email"},
    )
    return page


def _meta_post(client, *, leadgen_id="meta-activity-1", page_id="page-activity"):
    body = json.dumps({"entry": [{"id": page_id, "changes": [{
        "field": "leadgen", "value": {"leadgen_id": leadgen_id, "form_id": "form-activity"},
    }]}]}).encode()
    signature = hmac.new(b"signature-secret", body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhooks/meta-leads/", body, content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=f"sha256={signature}",
    )


META_DATA = {"form_id": "form-activity", "field_data": [
    {"name": "full_name", "values": ["Private Buyer"]},
    {"name": "phone_number", "values": ["9876543210"]},
    {"name": "email", "values": ["buyer@example.test"]},
]}


@override_settings(META_APP_SECRET="")
def test_meta_lead_success_links_crm_entity_without_answers(client, meta_page):
    with patch("apps.integrations.views.meta_leads._fetch_lead", return_value=META_DATA):
        response = _meta_post(client)
    assert response.status_code == 200
    activity = _activities("meta_leads").get()
    assert activity.status == "completed"
    assert activity.metadata["result"] == "created"
    assert activity.organization_id == meta_page.organization_id
    assert activity.connection_id == str(meta_page.pk)
    assert activity.entity_id == str(Lead.objects.get(organization=meta_page.organization).pk)
    _assert_private(activity)


@override_settings(META_APP_SECRET="")
def test_meta_http_acknowledgement_does_not_hide_fetch_failure(client, meta_page, caplog):
    with patch(
        "apps.integrations.views.meta_leads._fetch_lead",
        side_effect=requests.Timeout("https://graph.facebook.com/?access_token=private-token"),
    ):
        response = _meta_post(client)
    assert response.status_code == 200
    assert response.content == b"EVENT_RECEIVED"
    activity = _activities("meta_leads").get()
    assert activity.status == "failed"
    assert activity.error_code == "provider_fetch_failed"
    assert activity.error_category == "provider"
    assert activity.organization_id == meta_page.organization_id
    assert activity.provider_reference == "meta-activity-1"
    _assert_private(activity)
    assert "private-token" not in caplog.text


@override_settings(META_APP_SECRET="")
def test_meta_crm_rollback_failure_is_durable(client, meta_page):
    def broken_import(**kwargs):
        Lead.objects.create(
            organization=kwargs["organization"], pipeline=kwargs["pipeline"],
            stage=kwargs["stage"], name="Private Buyer", phone=kwargs["phone"],
        )
        raise RuntimeError("private-token")

    with (
        patch("apps.integrations.views.meta_leads._fetch_lead", return_value=META_DATA),
        patch("apps.integrations.views.meta_leads.upsert_lead", side_effect=broken_import),
    ):
        assert _meta_post(client).status_code == 200
    assert not Lead.objects.filter(organization=meta_page.organization).exists()
    activity = _activities("meta_leads").get()
    assert activity.status == "failed"
    assert activity.error_code == "crm_import_failed"
    assert activity.organization_id == meta_page.organization_id
    _assert_private(activity)


@pytest.mark.parametrize("lead_data,code,status", [
    ({"form_id": "missing-form", "field_data": []}, "form_unconfigured", "blocked"),
    ({"form_id": "form-activity", "field_data": []}, "invalid_phone", "skipped"),
])
@override_settings(META_APP_SECRET="")
def test_meta_mapping_and_phone_skips_are_explicit(client, meta_page, lead_data, code, status):
    with patch("apps.integrations.views.meta_leads._fetch_lead", return_value=lead_data):
        assert _meta_post(client).status_code == 200
    activity = _activities("meta_leads").get()
    assert activity.status == status
    assert activity.error_code == code
    assert activity.organization_id == meta_page.organization_id
    assert not Lead.objects.filter(organization=meta_page.organization).exists()


@override_settings(META_APP_SECRET="signature-secret")
def test_meta_unconfigured_page_is_unattributed(client):
    assert _meta_post(client, page_id="unknown-page").status_code == 200
    activity = _activities("meta_leads").get()
    assert activity.status == "blocked"
    assert activity.error_code == "page_unconfigured"
    assert activity.organization_id is None


@override_settings(META_APP_SECRET="signature-secret")
def test_meta_invalid_signature_does_not_assign_unverified_tenant(client, meta_page):
    response = client.post("/webhooks/meta-leads/", {
        "entry": [{"id": meta_page.page_id}],
    }, content_type="application/json")
    assert response.status_code == 403
    activity = _activities("meta_leads").get()
    assert activity.status == "blocked"
    assert activity.error_code == "invalid_signature"
    assert activity.organization_id is None
