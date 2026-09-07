from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount
from apps.organizations.models import Organization
from services.channels.hosted_ignore_service import HostedIgnoreSyncResult


class SuperadminHostedIgnoreViewsTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            email="superadmin-ignore@example.com",
            password="test-password-123",
            name="Super Admin",
        )

        session = SessionStore()
        set_authenticated_user(session, self.superuser)
        session.save()
        self.client.cookies["shvya_superadmin_sessionid"] = session.session_key

        self.organization = Organization.objects.create(
            name="Hosted Ignore Org",
            settings={"hosted_account_enabled": True},
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sales",
            display_phone_number="+919999999999",
            phone_number_id="+919999999999",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _list_url(self, organization=None):
        organization = organization or self.organization
        return reverse(
            "superadmin-organization-hosted-ignore-list",
            kwargs={"organization_id": organization.id},
        )

    def test_management_page_is_organization_level_and_has_required_controls(self):
        response = self.client.get(self._list_url())

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync all existing chats")
        self.assertContains(response, "Download CSV")
        self.assertContains(response, "Delete / Reset Ignore List")
        self.assertContains(response, "+919999999999")

    @patch("apps.superadmin.hosted_ignore_views.sync_existing_hosted_chats")
    def test_sync_action_targets_selected_organization(self, sync_existing):
        sync_existing.return_value = HostedIgnoreSyncResult(
            account_count=1,
            contact_count=12,
        )
        url = reverse(
            "superadmin-organization-hosted-ignore-sync",
            kwargs={"organization_id": self.organization.id},
        )

        response = self.client.post(url)

        self.assertRedirects(response, self._list_url())
        sync_existing.assert_called_once_with(organization=self.organization)

    def test_csv_download_is_scoped_to_selected_organization(self):
        HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Included Contact",
            chat_id="included@c.us",
        )

        other_org = Organization.objects.create(name="Other Org")
        other_account = WhatsAppAccount.objects.create(
            organization=other_org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Other Hosted",
            display_phone_number="+918888888888",
            phone_number_id="+918888888888",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        HostedChatIgnoreContact.objects.create(
            organization=other_org,
            account=other_account,
            phone_number="+917777777777",
            contact_name="Excluded Contact",
            chat_id="excluded@c.us",
        )

        url = reverse(
            "superadmin-organization-hosted-ignore-download",
            kwargs={"organization_id": self.organization.id},
        )
        response = self.client.get(url)
        body = response.content.decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Included Contact", body)
        self.assertIn("+919876543210", body)
        self.assertNotIn("Excluded Contact", body)
        self.assertNotIn("+917777777777", body)

    def test_reset_deletes_only_selected_organization_snapshot(self):
        selected = HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919876543210",
            contact_name="Selected",
            chat_id="selected@c.us",
        )

        other_org = Organization.objects.create(name="Other Org")
        other_account = WhatsAppAccount.objects.create(
            organization=other_org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Other Hosted",
            display_phone_number="+918888888888",
            phone_number_id="+918888888888",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        retained = HostedChatIgnoreContact.objects.create(
            organization=other_org,
            account=other_account,
            phone_number="+917777777777",
            contact_name="Retained",
            chat_id="retained@c.us",
        )

        url = reverse(
            "superadmin-organization-hosted-ignore-reset",
            kwargs={"organization_id": self.organization.id},
        )
        response = self.client.post(url)

        self.assertRedirects(response, self._list_url())
        self.assertFalse(
            HostedChatIgnoreContact.objects.filter(pk=selected.pk).exists()
        )
        self.assertTrue(
            HostedChatIgnoreContact.objects.filter(pk=retained.pk).exists()
        )
