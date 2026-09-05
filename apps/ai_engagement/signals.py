"""AI engagement signal hooks.

WhatsApp inbound AI engagement is intentionally queued by
``services.channels.whatsapp_service.handle_inbound_message`` after the
inbound transaction commits.

Do not also queue it from a ``WhatsAppMessage`` post-save signal: the service
already owns webhook idempotency and scheduling from both places causes one
new inbound message to enqueue ``generate_ai_engagement_response`` twice.

Keeping this module import-safe avoids changing the app configuration while
making the service the single scheduling path for WhatsApp inbound messages.
"""
