"""99acres Push/Pull integration regression tests using synthetic buyer data."""
from __future__ import annotations

from unittest.mock import Mock, patch
from uuid import uuid4

from django.contrib.sessions.backends.db import SessionStore
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import get_session_cookie_name, set_authenticated_user
from apps.crm.models import Lead, LeadNote, Pipeline, Stage
from apps.integrations.acres99_models import Acres99Event, Acres99Integration, Acres99Receipt
from apps.integrations.services.acres99 import (
    Acres99ProtocolError, parse_pull_xml, parse_push_xml, sync_connection,
)
from apps.organizations.models import Organization


def push_xml(queries):
    import xml.etree.ElementTree as ET
    root = ET.Element("Xml")
    for item in queries:
        node = ET.SubElement(root, "Qry")
        for key, value in item.items():
            ET.SubElement(node, key).text = str(value)
    return ET.tostring(root, encoding="utf-8")


def enquiry(query_id="ACRES-1", phone="9876543210", product="LIST-1"):
    return {
        "QryId": query_id,
        "QryType": "Query",
        "RcvdOn": "2026-10-09 15:00:00",
        "ProdType": "LP-B",
        "ProdId": product,
        "Project": "Tower 7",
        "CmpctLabl": "2BHK in Noida",
        "QryInfo": "Would like to arrange a viewing",
        "Name": "Synthetic Buyer",
        "Email": "buyer@example.test",
        "Phone": phone,
    }


PULL_RESPONSE = b"""<?xml version="1.0"?>
<Xml ActionStatus="true">
 <Resp>
  <QryDtl TblId="PULL-123" ResType="S2M">
   <ProdId Status="Active" Type="LP-B">LIST-12</ProdId>
   <CmpctLabl>Residential apartment in Noida</CmpctLabl>
   <QryInfo>Interested in a viewing</QryInfo>
   <RcvdOn>2026/10/09 14:00:00</RcvdOn>
  </QryDtl>
  <CntctDtl>
   <Name>Pull Buyer</Name><Email>buyer-pull@example.test</Email><Phone>9811111111</Phone>
  </CntctDtl>
 </Resp>
</Xml>"""


class Acres99XmlTests(SimpleTestCase):
    def test_push_xml_reads_cdata_and_multiple_queries(self):
        xml = (
            b'<Xml><Qry><QryId>one</QryId><QryInfo><![CDATA[Budget < 1 crore]]></QryInfo>'
            b'<Phone>9876543210</Phone></Qry>'
            b'<Qry><QryId>two</QryId><Name>Person</Name></Qry></Xml>'
        )
        rows = parse_push_xml(xml)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["requirement"], "Budget < 1 crore")
        self.assertEqual(rows[1]["query_id"], "two")

    def test_pull_xml_reads_documented_nested_format(self):
        row = parse_pull_xml(PULL_RESPONSE)[0]
        self.assertEqual(row["query_id"], "PULL-123")
        self.assertEqual(row["product_type"], "LP-B")
        self.assertEqual(row["product_id"], "LIST-12")
        self.assertEqual(row["phone"], "9811111111")

    def test_empty_success_response_has_no_enquiries(self):
        self.assertEqual(
            parse_pull_xml(b'<Xml ActionStatus="true"><Resp /></Xml>'),
            [],
        )
        self.assertEqual(parse_pull_xml(b'<Xml ActionStatus="true"><Resp>  </Resp></Xml>'), [])

    def test_pull_error_code_is_handled_without_exposing_provider_message(self):
        with self.assertRaises(Acres99ProtocolError) as exc:
            parse_pull_xml(b'<Xml ActionStatus="false"><ErrorDetail><Code>ERROR-0001</Code>'
                           b'<Message>Secret stuff</Message></ErrorDetail></Xml>')
        self.assertIn("ERROR-0001", str(exc.exception))
        self.assertNotIn("Secret stuff", str(exc.exception))

    def test_rejects_doctype_and_oversized_xml(self):
        with self.assertRaises(Acres99ProtocolError):
            parse_push_xml(b'<!DOCTYPE Xml [<!ENTITY x "foo">]><Xml><Qry/></Xml>')
        with self.assertRaises(Acres99ProtocolError):
            parse_push_xml(b"x" * (2 * 1024 * 1024 + 1))


class Acres99IntegrationTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="99acres Example Organization")
        self.admin = User.objects.create_user(
            email="acres99-admin@example.test", password="test-pass",
            name="99acres Org Admin", organization=self.organization, role=User.Role.ADMIN,
        )
        self.agent = User.objects.create_user(
            email="acres99-agent@example.test", password="test-pass",
            name="99acres Agent", organization=self.organization, role=User.Role.AGENT,
        )
        self.superadmin = User.objects.create_superuser(
            email="acres99-superadmin@example.test", password=None, name="99acres Superadmin",
        )
        self.pipeline = Pipeline.objects.filter(organization=self.organization, is_active=True).first()
        if not self.pipeline:
            self.pipeline = Pipeline.objects.create(organization=self.organization, name="Sales")
        self.stage = self.pipeline.stages.filter(is_active=True).order_by("display_order").first()
        if not self.stage:
            self.stage = Stage.objects.create(pipeline=self.pipeline, name="New Lead", display_order=1)

    def dashboard_login(self, user=None):
        session = SessionStore()
        set_authenticated_user(session, user or self.admin)
        session.save()
        self.client.cookies[get_session_cookie_name("dashboard")] = session.session_key

    def superadmin_login(self):
        session = SessionStore()
        set_authenticated_user(session, self.superadmin)
        session.save()
        self.client.cookies[get_session_cookie_name("superadmin")] = session.session_key

    def provision(self, *, mode="push"):
        integration = Acres99Integration.objects.create(
            organization=self.organization, pipeline=self.pipeline, stage=self.stage,
            mode=mode,
        )
        integration.generate_webhook_token()
        if mode in {"pull", "both"}:
            integration.set_credentials("user@example.test", "demo-password")
            integration.set_provider_token("test99acresProviderToken")
        integration.is_enabled = True
        integration.full_clean()
        integration.save()
        return integration

    def push(self, connection, queries):
        return self.client.post(
            reverse("acres99-webhook", kwargs={"token": connection.webhook_token}),
            data=push_xml(queries),
            content_type="application/xml",
        )

    def test_org_admin_requests_setup_but_cannot_provision(self):
        self.dashboard_login()
        response = self.client.post(reverse("crm-connect-hub-99acres"), {"action": "request_setup"})
        self.assertEqual(response.status_code, 302)
        connection = Acres99Integration.objects.get(organization=self.organization)
        self.assertFalse(connection.is_enabled)
        self.assertIsNone(connection.webhook_token)
        response = self.client.post(reverse("crm-connect-hub-99acres"), {"action": "generate"})
        self.assertEqual(response.status_code, 400)
        connection.refresh_from_db()
        self.assertIsNone(connection.webhook_token)

    def test_agents_cannot_read_or_change_configuration(self):
        connection = self.provision(mode="both")
        self.dashboard_login(self.agent)
        page = self.client.get(reverse("crm-connect-hub-99acres"))
        self.assertEqual(page.status_code, 403)
        result = self.client.post(reverse("crm-connect-hub-99acres"), {"action": "request_setup"})
        self.assertEqual(result.status_code, 403)
        self.assertNotIn(b"demo-password", page.content)
        connection.refresh_from_db()
        self.assertTrue(connection.is_enabled)

    def test_superadmin_provisions_and_rotates_per_organization_url(self):
        self.superadmin_login()
        url = reverse("superadmin-organization-99acres", kwargs={"organization_id": self.organization.pk})
        payload = {
            "action": "generate", "mode": "both",
            "pipeline_id": str(self.pipeline.pk), "stage_id": str(self.stage.pk),
            "account_username": "99-user", "account_password": "provider-password",
            "provider_token": "test99acresProviderToken",
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, 302)
        connection = Acres99Integration.objects.get(organization=self.organization)
        self.assertTrue(connection.is_enabled)
        self.assertIsNotNone(connection.webhook_token)
        self.assertTrue(connection.has_credentials)
        self.assertNotIn("provider-password", connection.encrypted_password)
        before = connection.webhook_token
        payload.update({"action": "rotate", "account_username": "", "account_password": "", "provider_token": ""})
        self.assertEqual(self.client.post(url, payload).status_code, 302)
        connection.refresh_from_db()
        self.assertNotEqual(before, connection.webhook_token)
        self.assertEqual(connection.get_credentials(), ("99-user", "provider-password"))
        self.assertEqual(connection.get_provider_token(), "test99acresProviderToken")
        self.assertEqual(
            self.client.post(
                reverse("acres99-webhook", kwargs={"token": before}),
                push_xml([enquiry()]), content_type="application/xml",
            ).status_code, 404,
        )

    def test_push_xml_creates_lead_notes_property_and_acknowledges_query(self):
        connection = self.provision()
        with patch("services.crm.lead_service._schedule_new_lead_welcome") as welcome:
            response = self.push(connection, [enquiry()])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<QryId>ACRES-1</QryId>", response.content)
        self.assertIn(b"<status>Y</status>", response.content)
        lead = Lead.objects.get(organization=self.organization)
        self.assertEqual(lead.phone, "+919876543210")
        self.assertEqual(lead.lead_source, "99acres")
        self.assertEqual(lead.attributes["acres99_property_id"], "LIST-1")
        self.assertEqual(lead.attributes["acres99_project"], "Tower 7")
        self.assertEqual(lead.pipeline_id, self.pipeline.id)
        self.assertEqual(lead.stage_id, self.stage.id)
        welcome.assert_not_called()
        self.assertEqual(Acres99Receipt.objects.get(integration=connection).external_query_id, "ACRES-1")

    def test_duplicate_query_does_not_mutate_qualified_lead(self):
        connection = self.provision()
        self.assertEqual(self.push(connection, [enquiry()]).status_code, 200)
        lead = Lead.objects.get(organization=self.organization)
        later = self.pipeline.stages.filter(name="Qualified").first()
        if later is None:
            later = Stage.objects.create(pipeline=self.pipeline, name="Qualified", display_order=90)
        lead.stage = later
        lead.save(update_fields=["stage"])
        updated = enquiry(product="DIFFERENT-LIST")
        updated["Name"] = "Overwrite attempt"
        self.assertEqual(self.push(connection, [updated]).status_code, 200)
        lead.refresh_from_db()
        self.assertEqual(lead.stage_id, later.id)
        self.assertEqual(lead.name, "Synthetic Buyer")
        self.assertEqual(lead.attributes["acres99_property_id"], "LIST-1")
        self.assertEqual(Acres99Receipt.objects.filter(integration=connection).count(), 1)
        self.assertEqual(Acres99Event.objects.filter(status="duplicate").count(), 1)

    def test_distinct_queries_for_same_phone_create_separate_property_notes(self):
        connection = self.provision()
        self.push(connection, [enquiry()])
        self.push(connection, [enquiry(query_id="ACRES-2", product="LIST-2")])
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(Acres99Receipt.objects.filter(integration=connection).count(), 2)
        note = LeadNote.objects.get(lead__organization=self.organization, note__contains="LIST-2")
        self.assertIn("ACRES-2", note.note)

    def test_partial_batch_only_acknowledges_successful_ids_and_retries_failure(self):
        connection = self.provision()
        response = self.push(connection, [enquiry(), enquiry(query_id="ACRES-BAD", phone="invalid")])
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"<QryId>ACRES-1</QryId><status>Y</status>", response.content)
        self.assertIn(b"<QryId>ACRES-BAD</QryId><status>N</status>", response.content)
        self.assertEqual(Lead.objects.filter(organization=self.organization).count(), 1)
        self.assertEqual(Acres99Receipt.objects.count(), 1)
        retry = self.push(connection, [enquiry(query_id="ACRES-BAD", phone="9876543211")])
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Acres99Receipt.objects.count(), 2)

    def test_lead_deletion_preserves_minimal_receipt_and_removes_contact_reference(self):
        connection = self.provision()
        self.assertEqual(self.push(connection, [enquiry()]).status_code, 200)
        lead = Lead.objects.get(organization=self.organization)
        lead.delete()
        receipt = Acres99Receipt.objects.get(integration=connection)
        event = Acres99Event.objects.get(integration=connection, status="created")
        self.assertIsNone(receipt.lead_id)
        self.assertIsNone(event.lead_id)
        self.assertEqual(receipt.external_query_id, "ACRES-1")
        self.assertEqual(receipt.property_id, "LIST-1")
        self.assertFalse(Lead.objects.filter(organization=self.organization).exists())

    def test_unknown_and_paused_tokens_cannot_create_leads(self):
        connection = self.provision()
        self.assertEqual(self.client.post(
            reverse("acres99-webhook", kwargs={"token": uuid4()}),
            push_xml([enquiry()]), content_type="application/xml",
        ).status_code, 404)
        connection.is_enabled = False
        connection.save(update_fields=["is_enabled"])
        self.assertEqual(self.push(connection, [enquiry()]).status_code, 404)
        self.assertFalse(Lead.objects.filter(organization=self.organization).exists())

    def test_pull_uses_encrypted_credentials_and_syncs_once(self):
        connection = self.provision(mode="pull")
        mocked = Mock(status_code=200, content=PULL_RESPONSE)
        with patch("apps.integrations.services.acres99.requests.post", return_value=mocked) as post:
            result = sync_connection(connection.pk, manual=True)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["imported"], 1)
        args, kwargs = post.call_args
        self.assertEqual(args[0].split("/")[2], "www.99acres.com")
        self.assertIn("/test99acresProviderToken/uid/", args[0])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertIn("<pswd>demo-password</pswd>", kwargs["data"]["xml"])
        lead = Lead.objects.get(organization=self.organization)
        self.assertEqual(lead.lead_source, "99acres")
        self.assertEqual(lead.phone, "+919811111111")
        connection.refresh_from_db()
        self.assertIsNotNone(connection.sync_cursor)
        self.assertEqual(connection.poll_hour_count, 1)

    def test_empty_pull_window_advances_cursor(self):
        connection = self.provision(mode="pull")
        with patch("apps.integrations.services.acres99.requests.post", return_value=Mock(
            status_code=200, content=b'<Xml ActionStatus="true"><Resp /></Xml>',
        )):
            result = sync_connection(connection.pk, manual=True)
        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["imported"], 0)
        connection.refresh_from_db()
        self.assertIsNotNone(connection.sync_cursor)
        self.assertEqual(Acres99Receipt.objects.count(), 0)

    def test_pull_retries_whole_window_after_unprocessable_query(self):
        connection = self.provision(mode="pull")
        bad = PULL_RESPONSE.replace(b"9811111111", b"bad-phone")
        with patch("apps.integrations.services.acres99.requests.post", return_value=Mock(status_code=200, content=bad)):
            result = sync_connection(connection.pk, manual=True)
        self.assertEqual(result["status"], "partial")
        connection.refresh_from_db()
        self.assertIsNone(connection.sync_cursor)
        self.assertEqual(Acres99Receipt.objects.count(), 0)

    def test_rate_limit_applies_to_manual_and_scheduled_pull(self):
        connection = self.provision(mode="pull")
        with patch("apps.integrations.services.acres99.requests.post", return_value=Mock(
            status_code=200, content=b'<Xml ActionStatus="true"><Resp/></Xml>',
        )) as post:
            for i in range(6):
                self.assertIn(sync_connection(connection.pk, manual=True)["status"], {"partial", "synced"})
            self.assertEqual(sync_connection(connection.pk, manual=True)["status"], "rate_limited")
            self.assertEqual(post.call_count, 6)

    def test_provider_failure_does_not_advance_sync_cursor(self):
        connection = self.provision(mode="pull")
        with patch("apps.integrations.services.acres99.requests.post", return_value=Mock(
            status_code=200,
            content=b'<Xml ActionStatus="false"><ErrorDetail><Code>ERROR-0001</Code></ErrorDetail></Xml>',
        )):
            self.assertEqual(sync_connection(connection.pk, manual=True)["status"], "failed")
        connection.refresh_from_db()
        self.assertIsNone(connection.sync_cursor)
        self.assertIn("ERROR-0001", connection.last_error)
