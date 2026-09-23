import asyncio

from channels.layers import InMemoryChannelLayer
from django.test import SimpleTestCase


class MultiASGIFanoutTests(SimpleTestCase):
    async def test_two_logical_consumers_receive_tenant_fanout_without_cross_tenant_leak(self):
        layer = InMemoryChannelLayer()
        first = await layer.new_channel("consumer.one")
        second = await layer.new_channel("consumer.two")
        foreign = await layer.new_channel("consumer.foreign")
        tenant_group = "whatsapp_inbox_11111111-1111-1111-1111-111111111111"
        foreign_group = "whatsapp_inbox_22222222-2222-2222-2222-222222222222"
        await layer.group_add(tenant_group, first)
        await layer.group_add(tenant_group, second)
        await layer.group_add(foreign_group, foreign)

        event = {"type": "whatsapp.inbox_update", "lead_id": "lead-1"}
        await layer.group_send(tenant_group, event)

        self.assertEqual(await layer.receive(first), event)
        self.assertEqual(await layer.receive(second), event)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(layer.receive(foreign), timeout=0.01)
