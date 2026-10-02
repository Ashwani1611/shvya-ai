# Hosted inbox live UI contract

This supersedes the Hosted pending-message display policy in `ai-engagement-queue.md`.
It does not change Cloud API pending visibility, provider send eligibility, the
45-second AI send gate, business hours, or actual delivery acknowledgement rules.

## Incoming and AI messages

Every committed live Hosted `WhatsAppMessage.save()` publishes a small account-scoped
`hosted.message` event, including the display-safe bubble and conversation preview.
It is emitted from HTTP callbacks and Celery workers alike. Database rollbacks emit
nothing. The publisher reads the final committed identity/status, excludes history
imports and hidden system chats, and never includes raw transport payloads, tokens
or storage paths. The consumer checks organization and account before forwarding.

The browser inserts/updates that bubble directly. It does not wait for the full
inbox snapshot request to finish. Queued and sending AI messages are visible with
explicit pending labels, never a false sent tick. Cancelled unsent drafts disappear;
real provider failures remain visible. Actual sent/delivered/read states still come
from the existing transport/acknowledgement path. UI notification failure must not
cause a provider retry or alter a committed delivery status.

## Recovery and ordering

Heartbeat renews Redis group membership and checks account access; a surviving TCP
socket alone is not sufficient after a Redis restart. Visible browser tabs also
reconcile snapshots after two seconds without a completed refresh, even if a socket
looks open. Requests are serialized; hidden tabs do not poll. Snapshot work remains
bounded by the existing query/page contract and must be monitored under load.

Per-message versions protect socket changes from snapshots already in flight. Duplicate
or old events do not append duplicate bubbles or downgrade a newer receipt. A fresh
latest-page snapshot removes obsolete drafts without discarding earlier pages loaded
by the user. Slow responses from previously selected chats remain fenced out. Status
updates do not recreate media players or disturb the user's scroll anchor.

Scripts have versioned URLs so the first reload after deployment gets the new client.
An already-open tab must reload once to install new JavaScript; normal incoming/outgoing
messages must subsequently appear without page refresh.

## Validation and rollout

Run `pytest apps/channels/tests/test_hosted_live_push.py
apps/channels/tests/test_hosted_consumer_heartbeat.py
apps/channels/tests/test_hosted_inbox_reliability.py tests/test_hosted_live_state.py
 tests/browser/test_hosted_chat_browser.py` as one command with repository test settings.
Node state tests also run independently with `node --test tests/test_hosted_live_state.cjs`.
Browser tests intercept all network/provider traffic and use real production JS/template.
They do not establish a production recipient-phone delivery result.

Deploy web, WebSocket and worker application services together and collect static files.
No database migration or gateway source change is needed. Do not bulk-reset/resend
old messages. Production acceptance requires a fresh authorized inbound and AI response,
checking that the same persisted IDs appear once in the active UI with correct status.
