from datetime import timedelta
from unittest.mock import patch

from django.contrib.sessions.backends.db import SessionStore
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.session_utils import set_authenticated_user
from apps.channels.models import WhatsAppAccount, WhatsAppMessage
from apps.crm.models import Lead, Pipeline, Stage
from apps.organizations.models import Organization


class WhatsAppAPI24HourSeparationTests(TestCase):
    def setUp(self):
        self.org = Organization.objects.create(
            name="API Window Separation Org",
            settings={"hosted_account_enabled": True},
        )
        self.user = User.objects.create_user(
            email="api-window@example.com",
            password="test-password",
            name="API Window Admin",
            organization=self.org,
            role=User.Role.ADMIN,
        )
        self.pipeline = Pipeline.objects.create(
            organization=self.org,
            name="Sales",
            owner=self.user,
        )
        self.stage = Stage.objects.create(
            pipeline=self.pipeline,
            name="New",
            display_order=1,
        )
        self.lead = Lead.objects.create(
            organization=self.org,
            pipeline=self.pipeline,
            stage=self.stage,
            name="Window Lead",
            phone="+919777777777",
        )
        self.api_account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type=WhatsAppAccount.ConnectionType.API,
            phone_number_id="meta-api-window",
            display_phone_number="+919000001234",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.hosted_account = WhatsAppAccount.objects.create(
            organization=self.org,
            connection_type="hosted",
            phone_number_id="+919000005678",
            display_phone_number="+919000005678",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

        old_api = WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.api_account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            from_number=self.lead.phone,
            to_number=self.api_account.display_phone_number,
            body="Old Meta inbound",
            status=WhatsAppMessage.Status.RECEIVED,
        )
        WhatsAppMessage.objects.filter(pk=old_api.pk).update(
            created_at=timezone.now() - timedelta(hours=25)
        )
        WhatsAppMessage.objects.create(
            organization=self.org,
            account=self.hosted_account,
            lead=self.lead,
            direction=WhatsAppMessage.Direction.INBOUND,
            from_number=self.lead.phone,
            to_number=self.hosted_account.display_phone_number,
            body="Fresh Hosted inbound",
            status=WhatsAppMessage.Status.RECEIVED,
        )

        session = SessionStore()
        set_authenticated_user(session, self.user)
        session.save()
        self.client.cookies["shvya_crm_sessionid"] = session.session_key

    @patch("apps.channels.whatsapp_api_chat_ui.send_whatsapp_message_task.delay")
    def test_fresh_hosted_reply_does_not_unlock_meta_freeform_send(self, delay):
        response = self.client.post(
            reverse("whatsapp-send-message", args=[self.lead.id]),
            {"body": "Should require template"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("24 hours", response.json()["error"])
        delay.assert_not_called()
        self.assertFalse(
            WhatsAppMessage.objects.filter(
                lead=self.lead,
                account=self.api_account,
                body="Should require template",
            ).exists()
        )
