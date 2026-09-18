"""Teams connection labels and pinned settings links must agree with the backend."""

from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase, override_settings
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.connection_attempts import WhatsAppConnectionAttempt
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.channels.providers.whatsapp import WhatsAppAPIError
from apps.crm.models import Pipeline
from apps.organizations.models import Organization
from services.channels.whatsapp_coexistence_service import complete_coexistence_signup
from services.teams.whatsapp_connections import member_whatsapp_connections


class TeamMemberWhatsAppTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Teams connection tests")
        self.admin = User.objects.create_user(
            email="teams-admin@example.com", password="test-password",
            name="Admin", organization=self.org, role=User.Role.ADMIN,
        )
        self.member = User.objects.create_user(
            email="teams-member@example.com", password="test-password",
            name="Member", organization=self.org, role=User.Role.AGENT,
            phone="+919876543210",
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org, name="Member pipeline", owner=self.member,
            country_code="+91", phone_number="9876543210",
        )
        self.login(self.admin)

    def login(self, user):
        session = SessionStore()
        set_authenticated_user(session, user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def account(self, **overrides):
        data = {
            "organization": self.org,
            "connection_type": WhatsAppAccount.ConnectionType.API,
            "display_phone_number": "+91 98765 43210",
            "phone_number_id": f"meta-{uuid4()}",
            "status": WhatsAppAccount.Status.CONNECTED,
            "is_active": True,
        }
        data.update(overrides)
        return WhatsAppAccount.objects.create(**data)

    def connection(self):
        return member_whatsapp_connections(
            organization=self.org, members=[self.member],
        )[self.member.pk]

    def gear(self, account=None, member=None):
        url = reverse("crm-team-member-automation-settings", args=[(member or self.member).pk])
        return self.client.get(url, {"account": account.pk} if account else {})

    def test_api_number_is_normalized_and_linked_without_loading_credentials(self):
        account = self.account()
        with self.assertNumQueries(2):
            connection = self.connection()
        self.assertEqual(connection["account"].pk, account.pk)
        self.assertEqual(connection["label"], "WhatsApp API")
        self.assertEqual(connection["number"], "+919876543210")
        self.assertIn("access_token", connection["account"].get_deferred_fields())

    def test_hosted_legacy_enum_is_not_business_app_coexistence(self):
        self.account(connection_type=WhatsAppAccount.ConnectionType.coexisted)
        self.assertEqual(self.connection()["label"], "Hosted Account")

    def test_verified_coexistence_has_one_label_not_api_and_hosted_labels(self):
        account = self.account()
        WhatsAppConnectionAttempt.objects.create(
            organization=self.org, account=account,
            method=WhatsAppConnectionAttempt.Method.EMBEDDED,
            status=WhatsAppConnectionAttempt.Status.CONNECTED,
            stage="coexistence_connected",
        )
        self.assertEqual(self.connection()["label"], "WhatsApp Coexistence")
        response = self.client.get(reverse("crm-teams"))
        self.assertContains(response, 'data-whatsapp-connection="%s"' % account.pk, count=1)
        self.assertContains(response, "WhatsApp Coexistence")
        self.assertNotContains(response, ">Hosted Account<")

    def test_old_coexistence_sync_warning_is_recognized(self):
        account = self.account()
        WhatsAppConnectionAttempt.objects.create(
            organization=self.org, account=account,
            method="embedded", status="connected", stage="coexistence_sync_warning",
        )
        self.assertEqual(self.connection()["label"], "WhatsApp Coexistence")

    def test_failed_coexistence_attempt_does_not_relabel_api(self):
        account = self.account()
        WhatsAppConnectionAttempt.objects.create(
            organization=self.org, account=account,
            method="embedded", status="failed", stage="coexistence_sync_warning",
        )
        self.assertEqual(self.connection()["label"], "WhatsApp API")

    def test_legacy_coexistence_messages_supply_provenance(self):
        account = self.account()
        WhatsAppMessage.objects.create(
            organization=self.org, account=account, external_id="wamid.teams-legacy",
            direction=WhatsAppMessage.Direction.OUTBOUND,
            from_number="+919876543210", to_number="+919999999999",
            status=WhatsAppMessage.Status.SENT,
            media_payload={"coexistence_sync": True, "historical": True},
        )
        self.assertEqual(self.connection()["label"], "WhatsApp Coexistence")

    def test_unconnected_states_never_expose_settings_gear(self):
        for status, active in (("pending", True), ("failed", True), ("disconnected", True), ("connected", False)):
            with self.subTest(status=status, active=active):
                account = self.account(status=status, is_active=active)
                self.assertEqual(self.connection()["label"], "Not connected")
                response = self.client.get(reverse("crm-teams"))
                self.assertNotContains(response, f"?account={account.pk}")
                account.delete()

    def test_duplicate_records_choose_one_newest_connected_account(self):
        self.account()
        current = self.account(connection_type="hosted")
        self.account(status="disconnected", is_active=False)
        result = self.connection()
        self.assertEqual(result["account"].pk, current.pk)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["label"], "Hosted Account")

    def test_graph_object_id_cannot_be_mistaken_for_a_phone_number(self):
        self.account(display_phone_number="", phone_number_id="919876543210")
        self.assertIsNone(self.connection()["account"])

    def test_hosted_can_use_legacy_number_field(self):
        self.account(connection_type="hosted", display_phone_number="", phone_number_id="+919876543210")
        self.assertEqual(self.connection()["label"], "Hosted Account")

    def test_inactive_pipeline_cannot_link_member(self):
        self.account()
        self.pipeline.is_active = False
        self.pipeline.save(update_fields=["is_active"])
        self.assertIsNone(self.connection()["account"])

    def test_account_and_pipeline_must_belong_to_authenticated_org(self):
        other_org = Organization.objects.create(name="Other organization")
        self.account(organization=other_org)
        self.assertIsNone(self.connection()["account"])
        self.pipeline.organization = other_org
        self.pipeline.save(update_fields=["organization"])
        self.account()
        self.assertIsNone(self.connection()["account"])

    def test_phone_coincidence_does_not_replace_pipeline_ownership(self):
        self.account()
        self.pipeline.owner = self.admin
        self.pipeline.save(update_fields=["owner"])
        self.assertIsNone(self.connection()["account"])

    def test_member_phone_selects_correct_connection_from_multiple_pipelines(self):
        chosen = self.account()
        Pipeline.objects.create(
            organization=self.org, name="Second pipeline", owner=self.member,
            country_code="+91", phone_number="9999999999",
        )
        self.account(display_phone_number="+919999999999", connection_type="hosted")
        self.assertEqual(self.connection()["account"].pk, chosen.pk)

    def test_disconnected_selected_number_does_not_fall_back_to_another(self):
        self.account(status="disconnected")
        Pipeline.objects.create(
            organization=self.org, name="Second pipeline", owner=self.member,
            country_code="+91", phone_number="9999999999",
        )
        self.account(display_phone_number="+919999999999")
        self.assertIsNone(self.connection()["account"])

    def test_multiple_unselected_numbers_do_not_get_an_arbitrary_gear(self):
        self.account()
        Pipeline.objects.create(
            organization=self.org, name="Second pipeline", owner=self.member,
            country_code="+91", phone_number="9999999999",
        )
        self.account(display_phone_number="+919999999999")
        self.member.phone = ""
        self.member.save(update_fields=["phone"])
        result = self.connection()
        self.assertIsNone(result["account"])
        self.assertIn("Multiple", result["reason"])
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(self.gear().status_code, 200)  # Existing unpinned chooser remains available.

    def test_api_gear_opens_exact_api_account(self):
        account = self.account()
        response = self.gear(account)
        self.assertEqual(response.status_code, 302)
        location = urlparse(response.url)
        self.assertEqual(location.path, reverse("whatsapp-accounts"))
        self.assertEqual(parse_qs(location.query)["settings"], [str(account.pk)])

    def test_hosted_gear_opens_exact_hosted_account(self):
        account = self.account(connection_type="hosted")
        response = self.gear(account)
        location = urlparse(response.url)
        self.assertEqual(location.path, reverse("whatsapp-connect-hosted"))
        self.assertEqual(parse_qs(location.query)["settings"], [str(account.pk)])

    def test_coexistence_gear_uses_shared_api_settings_not_hosted(self):
        account = self.account()
        WhatsAppConnectionAttempt.objects.create(
            organization=self.org, account=account, method="embedded",
            status="connected", stage="coexistence_connected",
        )
        self.assertEqual(urlparse(self.gear(account).url).path, reverse("whatsapp-accounts"))

    def test_pinned_gear_is_revalidated_after_disconnection(self):
        account = self.account()
        account.status = "disconnected"
        account.save(update_fields=["status"])
        self.assertRedirects(self.gear(account), reverse("crm-teams"), fetch_redirect_response=False)

    def test_tampered_account_cannot_open_another_connection(self):
        self.account()
        unrelated = self.account(display_phone_number="+918888888888")
        self.assertRedirects(self.gear(unrelated), reverse("crm-teams"), fetch_redirect_response=False)
        response = self.client.get(
            reverse("crm-team-member-automation-settings", args=[self.member.pk]),
            {"account": "not-a-uuid"},
        )
        self.assertRedirects(response, reverse("crm-teams"), fetch_redirect_response=False)

    def test_other_organization_member_is_not_accessible(self):
        other_org = Organization.objects.create(name="Other organization")
        other_user = User.objects.create_user(
            email="other-team@example.com", password="test-password", name="Other",
            organization=other_org, role=User.Role.AGENT,
        )
        self.assertEqual(self.gear(member=other_user).status_code, 404)

    def test_member_status_is_independent_from_whatsapp_status(self):
        account = self.account()
        self.member.is_active = False
        self.member.save(update_fields=["is_active"])
        response = self.client.get(reverse("crm-teams"))
        self.assertContains(response, "Inactive")
        self.assertContains(response, "WhatsApp API")
        self.assertContains(response, f"?account={account.pk}")
        self.assertEqual(self.gear(account).status_code, 302)

    def test_agent_can_view_but_cannot_manage_whatsapp_settings(self):
        account = self.account()
        self.login(self.member)
        response = self.client.get(reverse("crm-teams"))
        self.assertNotContains(response, f"?account={account.pk}")
        self.assertRedirects(self.gear(account), reverse("crm-teams"), fetch_redirect_response=False)

    def test_empty_member_list_does_not_query(self):
        with self.assertNumQueries(0):
            self.assertEqual(member_whatsapp_connections(organization=self.org, members=[]), {})

    @override_settings(META_APP_ID="test-app", META_APP_SECRET="test-secret")
    def test_coexistence_signup_retains_label_without_a_browser_attempt(self):
        module = "services.channels.whatsapp_coexistence_service"
        with (
            patch(f"{module}.embedded_provider.exchange_code_for_access_token", return_value="test-token"),
            patch(f"{module}._resolve_signup_assets", return_value=("test-waba", "test-phone-id")),
            patch(f"{module}._phone_details_without_registration", return_value={"display_phone_number": "+919876543210"}),
            patch(f"{module}._coexistence_status", return_value={"is_on_biz_app": True}),
            patch(f"{module}.whatsapp_provider.subscribe_app_to_waba"),
            patch(f"{module}.request_smb_app_data_sync", return_value={"request_id": "test-sync"}),
        ):
            account, warning, _ = complete_coexistence_signup(organization=self.org, code="test-code")
        self.assertEqual(warning, "")
        self.assertEqual(account.connection_attempts.get().stage, "coexistence_connected")
        self.assertEqual(self.connection()["label"], "WhatsApp Coexistence")

    @override_settings(META_APP_ID="test-app", META_APP_SECRET="test-secret")
    def test_coexistence_sync_warning_keeps_existing_attempt_and_correct_label(self):
        attempt = WhatsAppConnectionAttempt.objects.create(
            organization=self.org, created_by=self.admin, method="embedded",
        )
        module = "services.channels.whatsapp_coexistence_service"
        with (
            patch(f"{module}.embedded_provider.exchange_code_for_access_token", return_value="test-token"),
            patch(f"{module}._resolve_signup_assets", return_value=("test-waba", "test-phone-id")),
            patch(f"{module}._phone_details_without_registration", return_value={"display_phone_number": "+919876543210"}),
            patch(f"{module}._coexistence_status", return_value={"is_on_biz_app": True}),
            patch(f"{module}.whatsapp_provider.subscribe_app_to_waba"),
            patch(f"{module}.request_smb_app_data_sync", side_effect=WhatsAppAPIError("Sync unavailable")),
        ):
            account, warning, _ = complete_coexistence_signup(organization=self.org, code="test-code", attempt=attempt)
        self.assertTrue(warning)
        self.assertEqual(account.connection_attempts.count(), 1)
        attempt.refresh_from_db()
        self.assertEqual(attempt.stage, "coexistence_sync_warning")
        self.assertEqual(self.connection()["label"], "WhatsApp Coexistence")
