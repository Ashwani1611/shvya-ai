from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Pipeline
from apps.organizations.models import Organization


class TeamMemberAutomationSettingsTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(
            name="Automation Org", settings={"hosted_account_enabled": True}
        )
        self.admin = User.objects.create_user(
            email="admin@example.com",
            password="test-password",
            name="Admin",
            organization=self.organization,
            role=User.Role.ADMIN,
        )
        self.member = User.objects.create_user(
            email="member@example.com",
            password="test-password",
            name="Member",
            organization=self.organization,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.organization,
            owner=self.member,
            name="Member pipeline",
            country_code="+91",
            phone_number="8700274739",
        )
        session = SessionStore()
        set_authenticated_user(session, self.admin)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    def _settings_url(self):
        return reverse("crm-team-member-automation-settings", args=[self.member.id])

    def test_opens_hosted_settings_only_for_member_linked_number(self):
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            display_phone_number="+91 87002 74739",
            phone_number_id="+918700274739",
            status=WhatsAppAccount.Status.CONNECTED,
        )

        response = self.client.get(self._settings_url())

        self.assertRedirects(
            response,
            f"{reverse('whatsapp-connect-hosted')}?settings={account.id}",
            fetch_redirect_response=False,
        )

    def test_opens_api_settings_only_for_member_linked_number(self):
        account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            display_phone_number="+918700274739",
            phone_number_id="meta-phone-id",
            status=WhatsAppAccount.Status.CONNECTED,
        )

        response = self.client.get(self._settings_url())

        self.assertRedirects(
            response,
            f"{reverse('whatsapp-accounts')}?owner={self.member.id}&settings={account.id}",
            fetch_redirect_response=False,
        )

    def test_unlinked_number_is_not_used_for_member_settings(self):
        WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            display_phone_number="+919999999999",
            status=WhatsAppAccount.Status.CONNECTED,
        )

        response = self.client.get(self._settings_url())

        self.assertRedirects(response, reverse("crm-teams"), fetch_redirect_response=False)

