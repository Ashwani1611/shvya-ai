"""Regression checks for sequence detail rendering with and without a sender."""

from django.template.loader import render_to_string
from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.followups.models import FollowupSequence
from apps.followups.views.web import _step_context
from apps.organizations.models import Organization


class SequenceDetailRenderTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.org = Organization.objects.create(name="Detail Render Org")
        cls.user = User.objects.create_user(
            email="sequence-render@example.test", organization=cls.org,
            password="render-test", name="Sequence Admin", role=User.Role.ADMIN,
        )
        cls.account = WhatsAppAccount.objects.create(
            organization=cls.org,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            business_name="Hosted", phone_number_id="+919999111222",
            display_phone_number="+919999111222",
            status=WhatsAppAccount.Status.CONNECTED, is_active=True,
        )

    def _render(self, sequence, hosted):
        ctx = _step_context(sequence)
        ctx.update({
            "is_followup_admin": True,
            "is_hosted_sequence": hosted,
            "is_instagram_sequence": False,
            "provider_label": "WhatsApp" if hosted else "WhatsApp API",
            "approved_template_count": 0,
        })
        return render_to_string("followups/sequence_edit.html", ctx)

    def test_unbound_hosted_draft_detail_renders(self):
        seq = FollowupSequence.objects.create(
            organization=self.org, name="Unbound draft",
            is_active=False, created_by=self.user,
        )
        html = self._render(seq, hosted=True)
        self.assertIn("Unbound draft", html)
        self.assertIn("Add WhatsApp", html)

    def test_bound_hosted_sequence_detail_renders(self):
        seq = FollowupSequence.objects.create(
            organization=self.org, name="Bound Hosted",
            whatsapp_account=self.account, created_by=self.user,
        )
        html = self._render(seq, hosted=True)
        self.assertIn("Bound Hosted", html)
        self.assertIn("Add WhatsApp", html)
