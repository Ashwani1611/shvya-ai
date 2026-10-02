from django.test import TestCase, override_settings
from django.utils import timezone

from apps.channels.models import WhatsAppAccount, WhatsAppMessage, WhatsAppTemplate
from apps.channels.tracking_models import (
    WhatsAppTemplateTrackedClick,
    WhatsAppTemplateTrackedLink,
)
from apps.organizations.models import Organization
from services.channels import template_service
from services.channels.template_cta_tracking import augment_tracked_cta_analytics


@override_settings(CTA_TRACKING_PUBLIC_BASE_URL="https://track.shvya.example")
class WhatsAppTemplateCTAAccuracyTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="CTA Accuracy")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.API,
            business_name="CTA Accuracy Business",
            phone_number_id="phone-cta-accuracy",
            waba_id="waba-cta-accuracy",
            access_token="cta-accuracy-token",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.template = WhatsAppTemplate.objects.create(
            organization=self.organization,
            account=self.account,
            name="tracked_call_accuracy",
            category=WhatsAppTemplate.Category.MARKETING,
            status=WhatsAppTemplate.Status.APPROVED,
            body="Would you like to call us?",
            buttons=[
                {
                    "type": "call_phone",
                    "text": "Call sales",
                    "phone_number": "+918700000000",
                }
            ],
            meta_template_id="meta-tracked-call-accuracy",
        )
        template_service.build_meta_payload(template=self.template)
        self.day = timezone.localdate()

    def _tracked_click(self, *, recipient, message_suffix, fingerprint):
        sent_at = timezone.now()
        message = WhatsAppMessage.objects.create(
            organization=self.organization,
            account=self.account,
            direction=WhatsAppMessage.Direction.OUTBOUND,
            external_id=f"wamid.{message_suffix}",
            from_number="911111111111",
            to_number=recipient,
            body="Would you like to call us?",
            status=WhatsAppMessage.Status.DELIVERED,
            sent_at=sent_at,
            media_payload={
                "transport": "template",
                "template_id": str(self.template.id),
                "template_name": self.template.name,
            },
        )
        link = WhatsAppTemplateTrackedLink.objects.create(
            organization=self.organization,
            account=self.account,
            template=self.template,
            message=message,
            meta_template_id=self.template.meta_template_id,
            template_name=self.template.name,
            button_path="button0",
            action_type=WhatsAppTemplateTrackedLink.ActionType.CALL,
            button_text="Call sales",
            phone_number="+918700000000",
            is_active=True,
            sent_at=sent_at,
        )
        WhatsAppTemplateTrackedClick.objects.create(
            link=link,
            fingerprint=fingerprint,
            user_agent="Mozilla/5.0",
        )
        return link

    def _provider_result(self, *, total=0, unique=0):
        clicks = (
            [
                {
                    "type": "url_button",
                    "button_content": "Call sales",
                    "count": total,
                }
            ]
            if total
            else []
        )
        unique_clicks = (
            [
                {
                    "type": "url_button",
                    "button_content": "Call sales",
                    "count": unique,
                }
            ]
            if unique
            else []
        )
        return {
            self.template.meta_template_id: {
                "template_id": self.template.meta_template_id,
                "days": [
                    {
                        "date": self.day.isoformat(),
                        "sent": 10,
                        "delivered": 10,
                        "read": 5,
                        "clicked": total,
                    }
                ],
                "totals": {
                    "sent": 10,
                    "delivered": 10,
                    "read": 5,
                    "clicked": total,
                },
                "rates": {
                    "delivered": 100.0,
                    "read": 50.0,
                    "clicked": (total * 10.0) if total else None,
                },
                "availability": {
                    "clicked": bool(total),
                    "unique_clicked": bool(unique),
                },
                "clicks": clicks,
                "unique_clicks": unique_clicks,
                "unique_click_total": unique,
                "source": "meta",
            }
        }

    def test_meta_and_shvya_counts_for_same_button_use_max_not_sum(self):
        for index in range(3):
            self._tracked_click(
                recipient=f"9198765432{index}",
                message_suffix=f"same-button-{index}",
                fingerprint=f"fingerprint-{index}",
            )

        item = augment_tracked_cta_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
            results=self._provider_result(total=5, unique=4),
        )[self.template.meta_template_id]

        self.assertEqual(item["totals"]["clicked"], 5)
        self.assertEqual(item["unique_click_total"], 4)
        self.assertEqual(len(item["clicks"]), 1)
        self.assertEqual(item["clicks"][0]["count"], 5)

    def test_unique_clicks_deduplicate_same_recipient_across_sends(self):
        self._tracked_click(
            recipient="919876543210",
            message_suffix="repeat-one",
            fingerprint="fingerprint-repeat-one",
        )
        self._tracked_click(
            recipient="919876543210",
            message_suffix="repeat-two",
            fingerprint="fingerprint-repeat-two",
        )

        item = augment_tracked_cta_analytics(
            account=self.account,
            template_ids=[self.template.meta_template_id],
            start_date=self.day,
            end_date=self.day,
            results=self._provider_result(),
        )[self.template.meta_template_id]

        self.assertEqual(item["totals"]["clicked"], 2)
        self.assertEqual(item["unique_click_total"], 1)
        self.assertEqual(item["unique_clicks"][0]["count"], 1)
