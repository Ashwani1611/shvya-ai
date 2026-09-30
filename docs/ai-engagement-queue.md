# WhatsApp AI queue and send guarantees

The queue persists work in PostgreSQL. Celery notifications wake that work;
Redis publication is not the source of truth for whether a reply is pending.

## Sending order and controls

- The connected WhatsApp account is the pacing boundary. Every welcome,
  AI reply, and AI bump-up shares a fixed minimum 45-second interval between
  successful sends. Separate organizations/numbers retain independent queues.
- Welcomes precede replies. Each class preserves arrival order. For API replies,
  the inbound timestamp is carried into the generated message so faster AI
  generation cannot let a newer conversation jump ahead.
- A burst of messages from one lead is coalesced into its newest unanswered
  inbound turn; the conversation context includes the earlier inbound messages.
  Different leads retain separate work items.
- Organization AI, pipeline/Automation AI Auto-Reply, stage AI, and lead AI must
  all allow a send. The current pipeline's connected number is authoritative.
  Controls are checked again immediately before delivery, including welcomes.
- Qualified leads can receive contextual replies when their controls allow AI.
  Qualification completion does not override a stage's AI OFF control.

At 45 seconds, one sender can deliver at most 80 AI messages per hour before
generation time, account health pauses, business rules, or provider delays.
Bulk welcome queues therefore take time to drain by design.

## Durable work and delivery

Hosted replies and new-lead welcomes use `HostedAutomationJob`. Welcomes are
intents until processing becomes due: enqueueing does not call the model and
does not create a chat bubble. The worker records its lease only after it
starts; publishing to Celery alone leaves the job queued. Account-level
serialization prevents overlapping generation/delivery jobs on that number.

Expired processing leases and failed broker publications are recovered by
`hosted.dispatch_due_ai`. Explicit transient generation/delivery failures are
retried with a bounded budget. Pacing and account-health waits return the job
to queued and do not consume that failure budget. Generated messages are
reused after retries, rather than generated twice.

`AIMessageSendState` is the final shared send gate. Its database reservation is
committed before provider I/O. An abandoned send reservation retains a
conservative lease/cooldown; a successful send records `sent_at` and opens the
next slot no sooner than 45 seconds later. No worker sleeps to implement pacing.

Hosted gateway requests carry the persisted WhatsApp message UUID. A
session-scoped Redis claim and an fsynced journal in the existing gateway
session volume record the request outcome without storing customer text or
media. Completed retries return the original provider message ID; concurrent
retries wait. Unknown outcomes or missing history for a known retry fail
closed and require investigation instead of blindly sending duplicates.
The journal is retained for seven days. Keep the gateway session volume when
recreating the service.

API delivery recovery includes queued replies, welcomes and bump-ups, even
when they are older than one hour. It excludes work owned by durable jobs.
Unknown Meta send outcomes are not replayed automatically.

## Inbox and analytics

Queued/in-flight automated messages are excluded from chat previews and
transcripts. The queue shows the actual pending work, with separate waiting,
processing, blocked and recovering states. Manual queued messages retain their
normal inbox behavior.

AI engagement counts successful outbound messages only. Welcome, bump-up and
ordinary reply categories are mutually exclusive, and total AI is their sum.
New sends are grouped by `sent_at`; historical records without that field use
their existing creation date because the exact historical send time cannot be
reconstructed safely.

## Release and verification

Deploy the application and Hosted gateway together. Drain schema-dependent
workers, apply the `channels` and `hosted_automation` migrations, and restart
web, ASGI, Beat, general, `ai_realtime`, and `hosted_ai` workers on the same
release. Keep the existing gateway session storage. Follow the repository's
staging-first promotion process.

Required verification before production promotion:

1. PostgreSQL migration checks and concurrency regressions pass in CI.
2. With isolated test leads, queue simultaneous welcomes and replies on one
   connected number; verify welcome-first/FIFO order and actual send timestamps
   at least 45 seconds apart. Verify a second number can progress independently.
3. Disable each AI control while work is queued; verify that delivery is blocked.
4. Confirm burst replies, a Qualified-stage inbound turn, worker restart and
   temporary broker/gateway failures retain recoverable work without duplicate
   sends or permanently misleading processing status.
5. Compare successful message records with inbox visibility and the three
   analytics categories. Do not test by sending to customer leads.

Live Celery/Redis health cannot be inferred from passing source-level tests.
Verify the deployed workers consume their configured queues and Beat recovery
is running before declaring the incident resolved.
