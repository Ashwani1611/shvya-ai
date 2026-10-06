# Instagram connection setup

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.


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
   `https://shvya-ai.com/dashboard/instagram/connect/return/`
4. Configure this webhook callback URL:
   `https://shvya-ai.com/webhooks/instagram/`
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


## Current inbox and CRM behavior

The current Instagram inbox follows the same customer-context principles as the WhatsApp inbox while preserving Meta-specific policy:

- one local `InstagramConversation` is scoped to organization + Instagram account + participant;
- the conversation may link to one CRM `Lead` through the optional `lead_id` relationship;
- “View in CRM” must route to that exact linked lead;
- images, audio, video and supported story/shared-media reply payloads are normalized into persisted message attachments/content rather than discarded;
- outbound messaging rechecks the current Meta reply-window eligibility instead of assuming that a previously open thread is still sendable;
- OAuth uses the dedicated Instagram App ID/Secret and signed webhook handling; WhatsApp Embedded Signup credentials are not substitutes.

The inbox must not create cross-organization lead links. Any automatic/new lead association must validate the organization and current account context before persisting the link.

## Automation settings and follow-ups

Organization admins can open **Automation Settings** from Instagram connection
settings or the Instagram Chats sidebar. These account controls are independent
of WhatsApp's pipeline/account controls:

- AI Auto-Reply: defaults on to preserve existing behavior; organization,
  pipeline, stage and lead AI permissions still apply.
- Auto Lead Creation: defaults on; disabling it retains inbox conversations and
  existing lead links while preventing automatic creation of new leads.
- Bump-Up Messages and Count: defaults off, maximum 1–10 per unanswered customer
  turn (also capped by the organization bump-up limit). Uses Instagram-only
  context and the existing AI bump-up prompt after an hour of silence.
- Auto Follow-up: defaults off; controls assigned Instagram Cadence sequences.
- Business Hours: uses the organization's timezone and supports overnight hours;
  equal start/end times mean all day.
- Active Conversation Delay: defers follow-ups after inbound or human outbound
  activity. Settings changes recalculate pending schedules without restarting
  completed steps.

Create a Cadence with **Use Instagram**, add Instagram text messages (up to 1000
characters), email or reminder steps, and assign it to a linked CRM lead. The
lead must have exactly one conversation on the connected Instagram account;
no phone number or WhatsApp sender is required. The usual per-lead follow-up
switch remains authoritative. Instagram sends use the existing durable inbox
queue and provider throttling, and advance only after a persisted sent/read
receipt. Unknown delivery outcomes pause for review rather than being replayed.

The existing customer reply-window checks apply at queue/claim time. Automation
controls are checked again before delivery. Expired windows pause the sequence;
review it after the customer messages again. No HUMAN_AGENT exception is used.

Deploy migration `followups.0009` before restarting web and workers. The existing
follow-up and bump-up periodic tasks also dispatch Instagram work; no new Beat
entry or secret is required. Existing WhatsApp sequences retain their sender and
settings. Operations configuration writes for Instagram Cadences currently use
the dashboard; the Operations MCP returns an explicit guidance error.
