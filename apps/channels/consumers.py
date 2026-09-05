"""
WebSocket consumer powering live updates on the WhatsApp Chats
page -- replaces the earlier full-page-reload polling approach.

One connection per open browser tab on /dashboard/whatsapp/chats/.

Groups joined on connect:

    whatsapp_inbox_<org_id>          always -- sidebar updates
                                      (unread counts, last message)
                                      for every lead in the org.

    whatsapp_thread_<lead_id>        only when a specific lead's
                                      thread is open -- live message
                                      delivery + status ticks for
                                      that conversation.

Server -> client events (see services/channels/whatsapp_service.py
and apps/channels/tasks.py for what sends these):

    whatsapp.message        a new inbound message arrived for the
                             open thread.
    whatsapp.status         an outbound message's delivery status
                             changed (sent/delivered/read/failed).
    whatsapp.inbox_update   a lead's sidebar row needs refreshing
                             (unread count / last message / time).

This socket is receive-only from the browser's side -- actually
sending a WhatsApp message still goes through the existing HTTP
`whatsapp-send-message` endpoint (keeps CSRF protection and Celery
queuing exactly as before). The socket only pushes updates back.
"""

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer


class WhatsAppChatConsumer(AsyncJsonWebsocketConsumer):

    async def connect(self):

        user = self.scope.get("crm_user")

        if user is None:
            # No valid CRM dashboard session -- see
            # apps/accounts/channels_middleware.py.
            await self.close(code=4001)
            return

        self.organization_id = str(user.organization_id)

        self.inbox_group = f"whatsapp_inbox_{self.organization_id}"

        lead_id = self.scope["url_route"]["kwargs"].get("lead_id")

        self.lead_group = None

        if lead_id:

            belongs = await self._lead_belongs_to_org(
                lead_id,
                user.organization_id,
            )

            if not belongs:
                # Don't leak whether the lead exists at all --
                # just refuse the connection.
                await self.close(code=4003)
                return

            self.lead_group = f"whatsapp_thread_{lead_id}"

        await self.channel_layer.group_add(
            self.inbox_group,
            self.channel_name,
        )

        if self.lead_group:

            await self.channel_layer.group_add(
                self.lead_group,
                self.channel_name,
            )

        await self.accept()

    async def disconnect(self, close_code):

        if hasattr(self, "inbox_group"):

            await self.channel_layer.group_discard(
                self.inbox_group,
                self.channel_name,
            )

        if getattr(self, "lead_group", None):

            await self.channel_layer.group_discard(
                self.lead_group,
                self.channel_name,
            )

    async def receive_json(self, content, **kwargs):
        # Intentionally a no-op -- see module docstring. Browser
        # doesn't send app-level messages over this socket today.
        pass

    # ========================================================
    # SERVER -> CLIENT EVENT HANDLERS
    #
    # Method name must match the "type" used in group_send(),
    # with dots replaced by underscores (Channels convention).
    # ========================================================

    async def whatsapp_message(self, event):

        await self.send_json(
            {
                "kind": "message",
                "message": event["message"],
            }
        )

    async def whatsapp_status(self, event):

        await self.send_json(
            {
                "kind": "status",
                "message_id": event["message_id"],
                "lead_id": event["lead_id"],
                "status": event["status"],
            }
        )

    async def whatsapp_inbox_update(self, event):

        await self.send_json(
            {
                "kind": "inbox_update",
                "lead_id": event["lead_id"],
                "unread_count": event["unread_count"],
                "last_message_at": event["last_message_at"],
                "last_message_preview": event.get(
                    "last_message_preview",
                    "",
                ),
            }
        )

    # ========================================================
    # HELPERS
    # ========================================================

    @database_sync_to_async
    def _lead_belongs_to_org(self, lead_id, organization_id):

        from apps.crm.models import Lead

        return Lead.objects.filter(
            id=lead_id,
            organization_id=organization_id,
        ).exists()