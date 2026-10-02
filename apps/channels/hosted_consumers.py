"""WebSocket consumer for the Hosted WhatsApp linked-device inbox."""

import asyncio
from contextlib import suppress

from asgiref.sync import sync_to_async
from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from apps.core.observability import increment
from apps.core.websocket_metrics import remove as remove_websocket_metric
from apps.core.websocket_metrics import touch as touch_websocket_metric


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
        self.organization_id = str(user.organization_id)
        self.group_name = f"hosted_whatsapp_{account_id}"
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        await self.accept()
        await self.send_json({"kind": "ready", "account_id": account_id})
        await sync_to_async(touch_websocket_metric, thread_sensitive=False)(
            "hosted_whatsapp", self.channel_name, self.organization_id
        )
        await sync_to_async(increment, thread_sensitive=False)(
            "websocket.connections", labels={"kind": "hosted_whatsapp"}
        )
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
        if hasattr(self, "organization_id"):
            await sync_to_async(remove_websocket_metric, thread_sensitive=False)(
                "hosted_whatsapp", self.channel_name, self.organization_id
            )

    async def receive_json(self, content, **kwargs):
        # Provider sends stay on the authenticated, CSRF-protected HTTP path.
        if isinstance(content, dict) and content.get("kind") == "ping":
            await self.send_json({"kind": "pong"})
        return None

    async def _renew_subscription(self):
        # Redis restarts/expiry can remove a group's members while its browser
        # socket is still OPEN. Renew membership, not just the TCP heartbeat.
        if not await self._account_belongs_to_org(self.account_id, self.organization_id):
            await self.close(code=4003)
            return False
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        return True

    async def _heartbeat_loop(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not await self._renew_subscription():
                    return
                await sync_to_async(touch_websocket_metric, thread_sensitive=False)(
                    "hosted_whatsapp", self.channel_name, self.organization_id,
                )
                await self.send_json({"kind": "heartbeat"})
        except asyncio.CancelledError:
            raise
        except Exception:
            # Force recovery instead of leaving an apparently healthy socket
            # with a dead subscription. HTTP polling remains independently live.
            with suppress(Exception):
                await self.close(code=1011)

    async def hosted_message(self, event):
        if (str(event.get("account_id")) != self.account_id
                or str(event.get("organization_id")) != self.organization_id):
            return
        await self.send_json({
            "kind": "message",
            **{key: event[key] for key in (
                "account_id", "chat_key", "aliases", "message_id", "updated_at",
                "operation", "message", "conversation",
            ) if key in event},
        })

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
