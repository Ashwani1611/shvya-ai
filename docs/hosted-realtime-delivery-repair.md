# Hosted real-time delivery repair

## Scope

Repairs verified code paths found while investigating delayed Hosted inbox updates,
AI drafts appearing without delivery, and queues stuck behind disconnected sessions.
This change does not establish the current health of any production server.

- Persist live messages and acknowledgements on the private gateway session volume;
  replay failed callbacks after an outage or gateway restart. Only the currently
  running local session can deliver its records. Announce current readiness before
  replay when necessary, rather than replaying stale lifecycle events.
- Bound live chat lookup, connection probes and fencing teardown. Restore disconnected
  sessions as well as failed sessions with backoff, preserving the configured number.
  Ignore callbacks from replaced clients and do not revive expired QR sessions.
- Disable the optional mark-chat-seen step on all text/URL/upload sends. Check actual
  connection state before claiming that a provider call can start.
- A local message ID is not success. After the existing idempotency journal has saved
  the provider ID, check that same message for a WhatsApp server acknowledgement.
  Replays consult the same request; uncertain sends are never blindly repeated.
- AI jobs retain their generated reply while disconnected or awaiting an ACK.
  ACK checks do not consume generation retry attempts. A five-minute ACK wait ends
  with an explicit unconfirmed-delivery error, allowing later account jobs to proceed.
  Read/delivered responses are preserved rather than reduced to sent.

## Verification

Run `node --test whatsapp_web_gateway/tests/realtime-delivery.test.js
whatsapp_web_gateway/tests/realtime-build.test.js
whatsapp_web_gateway/tests/send-idempotency.test.js` (as one shell command).
The build test applies the complete ordered Docker source-patch chain, verifies its
syntax and checks that text and uploaded-media paths both use the corrected boundary.
The dedicated Hosted delivery regressions workflow runs these checks on pull requests.

Run `pytest apps/channels/tests/test_hosted_realtime_delivery.py` using the repository's
test settings/dependencies. These six tests exercise the transport and durable queue
with explicit doubles; they do not contact WhatsApp. Existing pacing, tenant, media
permission and idempotency regressions remain required before rollout.

## Rollout and live acceptance

Rebuild the gateway image and application image together, then restart the Hosted
AI worker and relevant send/web workers through the existing deployment process.
Preserve the LocalAuth/session volume. Its `_callback_outbox` directory contains
private message payloads and must have the same access controls and retention
management as the existing authenticated browser profile. Do not serve it publicly.

Use an authorized test conversation on the running environment to verify inbound
arrival without refreshing the browser, an AI reply received on the phone, progress
from queued to server-acknowledged status, and recovery after a controlled reconnect.
Check worker/Beat health and gateway callback errors as well as the UI.

Do not bulk-reset sent/failed messages or delete request journals. Previously failed
or skipped jobs need evidence-based review before retry; this code change does not
retroactively prove whether old messages reached WhatsApp. An unavailable phone or
missing recipient delivery receipt must not be confused with a missing server ACK.
