# Instagram connection setup

SHVYA's Instagram inbox uses Meta's Instagram API with Instagram Login for professional Business and Creator accounts.

## Meta app setup

Instagram Login must use the dedicated **Instagram App ID** and **Instagram App Secret** shown by Meta under the Instagram API setup. Do not use the generic `META_APP_ID` / `META_APP_SECRET` pair that SHVYA may already use for WhatsApp Embedded Signup.

Configure the production environment with:

- `META_INSTAGRAM_APP_ID=<Instagram App ID>`
- `META_INSTAGRAM_APP_SECRET=<Instagram App Secret>`

In Meta App Dashboard:

1. Add/configure **Instagram API with Instagram Login**.
2. In the Instagram API setup, copy the Instagram App ID and Instagram App Secret into the two production variables above.
3. Add this exact OAuth redirect URL:
   `https://dashboard.shvya-ai.com/dashboard/instagram/connect/return/`
4. Configure this webhook callback URL:
   `https://dashboard.shvya-ai.com/webhooks/instagram/`
5. Use the existing `META_VERIFY_TOKEN` as the webhook verify token unless the deployment intentionally configures another Instagram verify token.
6. Request these Instagram Login scopes:
   - `instagram_business_basic`
   - `instagram_business_manage_messages`
7. Subscribe the Instagram webhook product to `messages` and `messaging_postbacks`.
8. Complete Meta access-level / App Review requirements before connecting customer accounts outside the app's roles/testers.
9. Put the Meta app in Live mode when the integration is ready for production customers.

## OAuth behavior

SHVYA sends the browser to `https://www.instagram.com/oauth/authorize` using the dedicated Instagram App ID, the exact SHVYA callback URI, `force_reauth=true`, and Instagram-only login. The callback queues token exchange and initial inbox synchronization in Celery.

Production intentionally refuses to fall back to the generic WhatsApp/Facebook Meta App ID. This prevents Instagram's generic "Sorry, this page isn't available" / invalid-platform failures caused by using the wrong OAuth client identifier.

## Runtime behavior

- Connected Instagram accounts, conversations, messages, OAuth attempts, and webhook deliveries are persisted in PostgreSQL.
- Instagram access tokens and one-time OAuth codes are encrypted at rest.
- The Chats page reads SHVYA's tenant-scoped persisted inbox rather than making synchronous Graph calls for every page load.
- Initial and stale inbox state is reconciled through Meta's Conversations API in Celery.
- Incoming messages are ingested through signed Meta webhooks and processed idempotently.
- Replies are queued locally before SHVYA calls Meta's Send API.
- Meta requires the Instagram user to have initiated the conversation before the professional account can reply through the Send API.
- The same Instagram professional account cannot be connected to multiple SHVYA organizations.
