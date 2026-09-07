from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.channels.hosted_ignore_models import HostedChatIgnoreContact
from apps.channels.models import WhatsAppAccount
from apps.organizations.models import Organization
from services.channels.hosted_ignore_service import HostedIgnoreSyncResult


class SuperadminHostedIgnoreViewsTests(TestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            email="superadmin-ignore@example.com",
            password="test-password",
            name="Superadmin",
        )
        self.organization = Organization.objects.create(
            name="Ignore List Client",
            settings={"hosted_account_enabled": True},
        )
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted Sales",
            phone_number_id="+918700274739",
            display_phone_number="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.client.force_login(self.superuser)

    def test_organization_detail_exposes_ignore_list_option_outside_edit(self):
        response = self.client.get(
            reverse(
                "superadmin-organization-detail",
                args=[self.organization.id],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Existing Chat Ignore List")
        self.assertContains(
            response,
            reverse(
                "superadmin-organization-hosted-ignore-list",
                args=[self.organization.id],
            ),
        )

    def test_ignore_list_page_shows_sync_controls_and_connected_account(self):
        response = self.client.get(
            reverse(
                "superadmin-organization-hosted-ignore-list",
                args=[self.organization.id],
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sync all existing chats")
        self.assertContains(response, "+918700274739")
        self.assertContains(response, "Ignored existing chats")

    @patch("apps.superadmin.hosted_ignore_views.sync_existing_hosted_chats")
    def test_sync_action_runs_for_selected_organization(self, sync_existing):
        sync_existing.return_value = HostedIgnoreSyncResult(
            account_count=1,
            contact_count=42,
        )

        response = self.client.post(
            reverse(
                "superadmin-organization-hosted-ignore-sync",
                args=[self.organization.id],
            )
        )

        self.assertRedirects(
            response,
            reverse(
                "superadmin-organization-hosted-ignore-list",
                args=[self.organization.id],
            ),
        )
        sync_existing.assert_called_once_with(organization=self.organization)

    def test_download_and_reset_are_scoped_to_organization(self):
        other_org = Organization.objects.create(
            name="Other Client",
            settings={"hosted_account_enabled": True},
        )
        other_account = WhatsAppAccount.objects.create(
            organization=other_org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            phone_number_id="+919000000001",
            display_phone_number="+919000000001",
            status=WhatsAppAccount.Status.CONNECTED,
        )
        own_contact = HostedChatIgnoreContact.objects.create(
            organization=self.organization,
            account=self.account,
            phone_number="+919811112222",
            contact_name="Own Existing Customer",
            chat_id="919811112222@c.us",
        )
        other_contact = HostedChatIgnoreContact.objects.create(
            organization=other_org,
            account=other_account,
            phone_number="+919822223333",
            contact_name="Other Existing Customer",
            chat_id="919822223333@c.us",
        )

        download = self.client.get(
            reverse(
                "superadmin-organization-hosted-ignore-download",
                args=[self.organization.id],
            )
        )
        csv_body = download.content.decode("utf-8")

        self.assertEqual(download.status_code, 200)
        self.assertIn("Own Existing Customer", csv_body)
        self.assertNotIn("Other Existing Customer", csv_body)

        reset = self.client.post(
            reverse(
                "superadmin-organization-hosted-ignore-reset",
                args=[self.organization.id],
            )
        )

        self.assertEqual(reset.status_code, 302)
        self.assertFalse(
            HostedChatIgnoreContact.objects.filter(pk=own_contact.pk).exists()
        )
        self.assertTrue(
            HostedChatIgnoreContact.objects.filter(pk=other_contact.pk).exists()
        )
