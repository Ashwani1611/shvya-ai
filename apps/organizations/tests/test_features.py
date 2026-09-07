from django.test import TestCase

from apps.organizations.features import (
    is_hosted_account_enabled,
    set_hosted_account_enabled,
)
from apps.organizations.models import Organization


class OrganizationFeatureFlagTests(TestCase):
    def test_hosted_account_is_disabled_by_default(self):
        organization = Organization.objects.create(name="Default Feature Org")

        self.assertFalse(is_hosted_account_enabled(organization))

    def test_hosted_account_toggle_persists_and_preserves_other_settings(self):
        organization = Organization.objects.create(
            name="Feature Toggle Org",
            settings={"existing_setting": {"enabled": True}},
        )

        set_hosted_account_enabled(organization, True)
        organization.refresh_from_db()

        self.assertTrue(is_hosted_account_enabled(organization))
        self.assertTrue(organization.settings["existing_setting"]["enabled"])

        set_hosted_account_enabled(organization, False)
        organization.refresh_from_db()

        self.assertFalse(is_hosted_account_enabled(organization))
        self.assertTrue(organization.settings["existing_setting"]["enabled"])
