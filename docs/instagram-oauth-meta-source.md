# Meta source of truth for Instagram Login

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.


SHVYA uses Meta's **Instagram API with Instagram Login** for professional Business/Creator accounts.

The integration intentionally uses:

- OAuth host: `https://www.instagram.com/oauth/authorize`
- Token exchange: `https://api.instagram.com/oauth/access_token`
- Graph host: `https://graph.instagram.com`
- Permissions: `instagram_business_basic`, `instagram_business_manage_messages`
- Dedicated Instagram App ID / Instagram App Secret from the Instagram API setup

The generic Meta/Facebook App ID used for WhatsApp Embedded Signup is not used as the Instagram Login `client_id` in production.

## Callback and account identity contract — 2026-10-01

Rechecked against Meta's [Business Login](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/business-login/),
[Get Started](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-instagram-login/get-started/),
[access token](https://developers.facebook.com/docs/instagram-platform/reference/access_token/),
and [webhook](https://developers.facebook.com/docs/instagram-platform/webhooks/) references.

- The authorization-code exchange and `/me` profile response can wrap the single account in `data: [{...}]`. SHVYA accepts that documented shape and the existing flat shape; malformed or multiple-account results fail visibly.
- `/me.user_id` is the professional account ID used for subscriptions, messaging and incoming webhook matching. `/me.id` can be app-scoped and must not take precedence.
- A successful code exchange does not prove that inbox setup succeeded. The long-lived token and positive expiry must be returned; the webhook subscription must explicitly return `success: true`; an initial conversation sync must complete.
- Reauthorization resets subscription/sync markers. A failed setup remains visible, and the connect page continues read-only polling after authorization while setup completes.
- When the token exchange returns granted permissions, both requested DM scopes must be present. An absent permissions field is accepted for compatibility with existing flat responses.
- Meta's `#_` fragment is normal after authorization and is not part of the code. The browser removes this cosmetic fragment on the connect page.
- Reconnecting the same account preserves its inbox. Replacing an account with existing conversation history is rejected unless Meta proves that the old identifier is an alias of the same account. This avoids assigning old messages to a different identity.

Regression coverage includes a signed callback followed by the documented token/profile envelopes, long-lived exchange, subscription and an empty successful inbox sync. No real customer messages are sent by these tests. Live verification still requires completing Meta authorization with an eligible professional account on the deployed revision.

## Second-pass lifecycle and inbox checks

- Older OAuth tasks are superseded when a newer attempt exists. Completed attempts cannot restore a disconnected account or relabel successful history as failed.
- Token refresh requires an unexpired token at least 24 hours old. Account locks and authorization-snapshot checks prevent stale refresh/setup results from overwriting a disconnect or newer credentials.
- Webhook dispatch records a five-minute lease. The existing Celery Beat/worker stack scans up to 100 stale pending/processing envelopes each minute; terminal failures stay visible for review. Processing uses late acknowledgements and durable event IDs so worker redelivery does not duplicate messages.
- Subscription includes `messaging_seen` with the existing profile/messaging permissions. Receipts update the named message or supplied watermark; echoes preserve read status. Deleted messages keep content-free tombstones and do not reopen the reply window.
- Sender/recipient, account and conversation checks protect message updates. Invalid conversation-list responses cannot falsely complete initial inbox setup.
- Connect and inbox polling resume after browser history restoration. Timeout/media errors remain visible, and loading older messages updates read state for the displayed page.

Deployment requires the additive `channels.0018_instagram_webhook_dispatch_lease` migration before the new web/worker code runs. This uses the existing worker and Beat services; no infrastructure/YAML changes are required. Local tests use an isolated SQLite harness; PostgreSQL concurrency checks, browser visual QA and live Meta authorization remain separate deployment verification gates.
