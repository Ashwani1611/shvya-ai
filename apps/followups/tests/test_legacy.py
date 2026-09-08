from django.test import TestCase

from apps.accounts.models import User
from apps.channels.models import WhatsAppAccount
from apps.crm.models import Lead, Pipeline, Stage
from apps.followups.models import FollowupSequence
from apps.organizations.models import Organization
from services.followup_service import FollowupError, assign_sequence, create_sequence


class PipelineSenderRoutingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Sequence Routing Org")
        self.user = User.objects.create_user(
            email="routing@example.com",
            organization=self.organization,
            password="test-password",
            name="Routing Admin",
            role=User.Role.ADMIN,
        )
        self.api_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="API Sender A",
            display_phone_number="+919999999999",
            phone_number_id="123456789",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        self.api_sequence = FollowupSequence.objects.create(
            organization=self.organization,
            name="API Sender A Sequence",
            whatsapp_account=self.api_account,
            created_by=self.user,
        )

    def _lead_for(self, number, name):
        pipeline = Pipeline.objects.create(
            organization=self.organization,
            name=name,
            phone_number=number,
            owner=self.user,
        )
        stage = Stage.objects.get(pipeline=pipeline, display_order=1)
        return Lead.objects.create(
            organization=self.organization,
            pipeline=pipeline,
            stage=stage,
            name=f"{name} Lead",
            phone="+919111111111",
        )

    def test_hosted_sequence_is_reusable_for_a_linked_hosted_pipeline(self):
        hosted_account = WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="Hosted Sender",
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            display_phone_number="+918888888888",
            phone_number_id="+918888888888",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )
        sequence = create_sequence(
            organization=self.organization,
            created_by=self.user,
            name="Reusable Hosted Sequence",
            description="",
            provider="hosted",
        )

        state = assign_sequence(
            lead=self._lead_for("+918888888888", "Hosted Pipeline"),
            sequence=sequence,
            actor=self.user,
        )

        self.assertEqual(sequence.whatsapp_account_id, hosted_account.id)
        self.assertEqual(state.sequence, sequence)

    def test_api_sequence_rejects_a_different_linked_api_number(self):
        WhatsAppAccount.objects.create(
            organization=self.organization,
            business_name="API Sender B",
            display_phone_number="+918888888888",
            phone_number_id="987654321",
            status=WhatsAppAccount.Status.CONNECTED,
            is_active=True,
        )

        with self.assertRaisesMessage(
            FollowupError,
            "Choose a sequence created for the pipeline's linked WhatsApp API number.",
        ):
            assign_sequence(
                lead=self._lead_for("+918888888888", "API Pipeline B"),
                sequence=self.api_sequence,
                actor=self.user,
            )
