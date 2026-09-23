from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from apps.channels.hosted_gateway_routing import (
    gateway_client_for_account,
    gateway_shard_for_account,
    move_hosted_account,
    record_gateway_presence,
)
from apps.channels.models import WhatsAppAccount
from apps.organizations.models import Organization


GATEWAYS = '{"east":"http://gateway-east:3000","west":"http://gateway-west:3000"}'


class HostedGatewayShardingTests(TestCase):
    def setUp(self):
        self.organization = Organization.objects.create(name="Shard Org", package="dfy")
        self.account = WhatsAppAccount.objects.create(
            organization=self.organization,
            connection_type=WhatsAppAccount.ConnectionType.coexisted,
            status=WhatsAppAccount.Status.PENDING,
            phone_number_id="hosted-shard-test",
            display_phone_number="+919999999999",
        )

    @patch.dict("os.environ", {"WHATSAPP_WEB_GATEWAYS": GATEWAYS}, clear=False)
    def test_assignment_is_deterministic_and_durable(self):
        shard = gateway_shard_for_account(self.account)
        self.assertIn(shard, {"east", "west"})
        self.account.refresh_from_db()
        self.assertEqual(self.account.hosted_gateway_shard, shard)
        self.assertEqual(gateway_shard_for_account(self.account), shard)

    @patch.dict("os.environ", {"WHATSAPP_WEB_GATEWAYS": GATEWAYS}, clear=False)
    def test_callback_from_wrong_shard_is_fenced(self):
        self.account.hosted_gateway_shard = "east"
        self.account.save(update_fields=["hosted_gateway_shard", "updated_at"])

        accepted = record_gateway_presence(
            account=self.account,
            payload={"gatewayShard": "west", "gatewayOwner": "west-1"},
            event="ready",
        )

        self.assertFalse(accepted)
        self.account.refresh_from_db()
        self.assertEqual(self.account.hosted_lease_owner, "")

    @patch.dict("os.environ", {"WHATSAPP_WEB_GATEWAYS": GATEWAYS}, clear=False)
    def test_matching_callback_persists_lease_health(self):
        self.account.hosted_gateway_shard = "east"
        self.account.save(update_fields=["hosted_gateway_shard", "updated_at"])

        accepted = record_gateway_presence(
            account=self.account,
            payload={"gatewayShard": "east", "gatewayOwner": "east-7"},
            event="gateway_heartbeat",
        )

        self.assertTrue(accepted)
        self.account.refresh_from_db()
        self.assertEqual(self.account.hosted_lease_owner, "east-7")
        self.assertIsNotNone(self.account.hosted_lease_expires_at)
        self.assertIsNotNone(self.account.hosted_gateway_heartbeat_at)

    @patch.dict("os.environ", {"WHATSAPP_WEB_GATEWAYS": GATEWAYS}, clear=False)
    def test_late_disconnect_cannot_clear_a_new_owners_lease(self):
        self.account.hosted_gateway_shard = "east"
        self.account.hosted_lease_owner = "east-new"
        self.account.hosted_lease_expires_at = timezone.now() + timedelta(seconds=60)
        self.account.save(
            update_fields=[
                "hosted_gateway_shard",
                "hosted_lease_owner",
                "hosted_lease_expires_at",
                "updated_at",
            ]
        )

        accepted = record_gateway_presence(
            account=self.account,
            payload={"gatewayShard": "east", "gatewayOwner": "east-old"},
            event="disconnected",
        )

        self.assertFalse(accepted)
        self.account.refresh_from_db()
        self.assertEqual(self.account.hosted_lease_owner, "east-new")

    @patch.dict("os.environ", {"WHATSAPP_WEB_GATEWAYS": GATEWAYS}, clear=False)
    def test_active_lease_blocks_manual_rebalance(self):
        self.account.hosted_gateway_shard = "east"
        self.account.hosted_lease_owner = "east-7"
        self.account.hosted_lease_expires_at = timezone.now() + timedelta(seconds=60)
        self.account.save(
            update_fields=[
                "hosted_gateway_shard",
                "hosted_lease_owner",
                "hosted_lease_expires_at",
                "updated_at",
            ]
        )

        with self.assertRaisesMessage(ValueError, "active gateway lease"):
            move_hosted_account(account=self.account, target_shard="west")

    @patch.dict("os.environ", {"WHATSAPP_WEB_GATEWAYS": GATEWAYS}, clear=False)
    def test_client_uses_the_persisted_shard_url(self):
        self.account.hosted_gateway_shard = "west"
        self.account.save(update_fields=["hosted_gateway_shard", "updated_at"])
        client = gateway_client_for_account(self.account)
        self.assertEqual(client.base_url, "http://gateway-west:3000")
