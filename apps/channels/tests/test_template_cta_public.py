import re

from django.test import TestCase

from apps.channels.models import WhatsAppAccount, WhatsAppTemplate
from apps.channels.template_cta_models import WhatsAppTemplateCTAEvent
from apps.organizations.models import Organization
from services.channels.template_cta_tracking import encode_cta_token


class TemplateCTAPublicTests(TestCase):
    def setUp(self):
        organization = Organization.objects.create(
            package="dfy",
            name="CTA Public Test",
        )
        account = WhatsAppAccount.objects.create(
            organization=organization,
            business_name="Test Sender",
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=organization,
            account=account,
            name="public_cta_receipt",
            body="Hello",
            status=WhatsAppTemplate.Status.APPROVED,
            meta_template_id="meta-public-cta",
        )

    @staticmethod
    def confirmation_token(response):
        match = re.search(
            rb'name="event_token" value="([^"]+)"',
            response.content,
        )
        if match is None:
            raise AssertionError("Confirmation token was not rendered.")
        return match.group(1).decode("utf-8")

    def test_get_does_not_count_and_repeated_post_is_idempotent(self):
        token = encode_cta_token(
            template=self.template,
            action_type="url",
            destination="https://example.test/offer",
            label="View offer",
            button_index=0,
        )
        path = f"/w/cta/{token}/"

        preview = self.client.get(path)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(WhatsAppTemplateCTAEvent.objects.count(), 0)

        form_token = self.confirmation_token(preview)
        for _ in range(2):
            response = self.client.post(
                path,
                {"event_token": form_token},
                HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.json()["destination"],
                "https://example.test/offer",
            )

        self.assertEqual(WhatsAppTemplateCTAEvent.objects.count(), 1)
