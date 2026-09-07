"""WebSocket consumer for the Hosted WhatsApp linked-device inbox."""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


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

    async def disconnect(self, close_code):
        if getattr(self, "group_name", None):
            await self.channel_layer.group_discard(
                self.group_name,
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        # Hosted chat sending remains on the CSRF-protected HTTP endpoint.
        return None

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
