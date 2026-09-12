import asyncio
from unittest.mock import AsyncMock, patch

from django.test import SimpleTestCase

from apps.channels.hosted_consumers import (
    HEARTBEAT_INTERVAL_SECONDS,
    HostedWhatsAppChatConsumer,
)


class HostedWhatsAppConsumerHeartbeatTests(SimpleTestCase):
    def test_heartbeat_interval_is_below_common_idle_timeout(self):
        self.assertLess(HEARTBEAT_INTERVAL_SECONDS, 60)

    async def test_heartbeat_loop_emits_keepalive_frame(self):
        consumer = HostedWhatsAppChatConsumer()
        consumer.send_json = AsyncMock()

        sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
        with patch("apps.channels.hosted_consumers.asyncio.sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await consumer._heartbeat_loop()

        consumer.send_json.assert_awaited_once_with({"kind": "heartbeat"})

    async def test_optional_client_ping_receives_pong(self):
        consumer = HostedWhatsAppChatConsumer()
        consumer.send_json = AsyncMock()

        await consumer.receive_json({"kind": "ping"})

        consumer.send_json.assert_awaited_once_with({"kind": "pong"})
