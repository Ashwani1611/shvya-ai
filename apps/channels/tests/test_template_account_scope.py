from types import SimpleNamespace

from django.test import TestCase

from apps.channels import template_ui
from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.organizations.models import Organization


class WhatsAppTemplateAccountScopeTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(name="Template Scope Org")
        self.other_org = Organization.objects.create(name="Other Template Org")
        self.user = SimpleNamespace(organization=self.org)

        self.active_api = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Active API",
            phone_number_id="api-active",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.disconnected_api = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Disconnected API",
            phone_number_id="api-disconnected",
            status=WhatsAppAccount.Status.DISCONNECTED,
            is_active=True,
        )
        self.pending_api = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Pending API",
            phone_number_id="api-pending",
            status=WhatsAppAccount.Status.PENDING,
            is_active=True,
        )
        self.inactive_api = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Inactive API",
            phone_number_id="api-inactive",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=False,
        )
        self.hosted = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted",
            phone_number_id="hosted-connected",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.other_api = WhatsAppAccount.objects.create(
            organization=self.other_org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="Other API",
            phone_number_id="other-api",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

    def _template(self, account, name):
        return WhatsAppTemplate.objects.create(
            organization=account.organization,
            account=account,
            name=name,
            body="Hello",
            category=WhatsAppTemplate.Category.UTILITY,
        )

    def test_account_choices_include_only_active_connected_api_accounts(self):
        self.assertEqual(list(template_ui._accounts(self.user)), [self.active_api])

    def test_template_scope_excludes_irrelevant_accounts(self):
        visible = self._template(self.active_api, "visible_template")
        hidden = [
            self._template(self.disconnected_api, "disconnected_template"),
            self._template(self.pending_api, "pending_template"),
            self._template(self.inactive_api, "inactive_template"),
            self._template(self.hosted, "hosted_template"),
            self._template(self.other_api, "other_org_template"),
        ]

        self.assertEqual(list(template_ui._templates(self.user)), [visible])
        for template in hidden:
            with self.subTest(template=template.name):
                self.assertIsNone(template_ui._template(self.user, template.id))
