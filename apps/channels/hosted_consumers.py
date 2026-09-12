"""WebSocket consumer for the Hosted WhatsApp linked-device inbox."""

import asyncio
from contextlib import suppress

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


HEARTBEAT_INTERVAL_SECONDS = 25


class HostedWhatsAppChatConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        user = self.scope.get("crm_user")
        if user is None:
            await self.close(code=4001)
            return

        account_id = str(self.scope["url_route"]["kwargs"].get("account_id") or "")
        if not account_id or not await self._account_belongs_to_org(
            account_id,
            user.organization_id,
        ):
            await self.close(code=4003)
            return

        self.account_id = account_id
        self.group_name = f"hosted_whatsapp_{account_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def disconnect(self, close_code):
        heartbeat_task = getattr(self, "heartbeat_task", None)
        if heartbeat_task:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task

        if getattr(self, "group_name", None):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        # Hosted chat sending remains on the CSRF-protected HTTP endpoint.
        # Accept an optional client ping as well so future clients can actively
        # confirm liveness without changing the chat transport contract.
        if isinstance(content, dict) and content.get("kind") == "ping":
            await self.send_json({"kind": "pong"})
        return None

    async def _heartbeat_loop(self):
        """Keep idle Hosted chat sockets alive through proxies/load balancers."""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self.send_json({"kind": "heartbeat"})
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed send means the connection is already unusable; Channels
            # will run disconnect cleanup while the browser reconnects normally.
            return

    async def hosted_refresh(self, event):
        await self.send_json(
            {
                "kind": "refresh",
                "reason": event.get("reason", "message"),
                "chat_key": event.get("chat_key", ""),
            }
        )

    @database_sync_to_async
    def _account_belongs_to_org(self, account_id, organization_id):
        from apps.channels.models import WhatsAppAccount

        return WhatsAppAccount.objects.filter(
            id=account_id,
            organization_id=organization_id,
            connection_type="hosted",
            is_active=True,
        ).exists()
