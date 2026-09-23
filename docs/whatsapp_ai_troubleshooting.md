# Diagnosing WhatsApp AI replies

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.

Docker images now default to `config.settings.prod`. If the VPS `.env` explicitly
sets `DJANGO_SETTINGS_MODULE`, use `config.settings.prod` there too. Recreate web,
worker, and beat after changing credentials or settings; restarting a container
alone does not load a changed Compose `env_file`.

The API webhook routes by the business phone number, not Meta's opaque
`phone_number_id`. Pipeline country code and number are normalized before matching.
Existing leads retain their CRM pipeline. Leads previously created in the fallback
pipeline must be reviewed and moved to the intended pipeline through the CRM.

Required for all WhatsApp transports:
- The lead's current pipeline is the sender authority. The pipeline's WhatsApp number must match the connected account used for inbound/outbound processing.
- Never troubleshoot a mismatch by falling back to another connected WhatsApp number; CRM chat links, AI replies, Cadence, Workflows and Bulk Campaigns all preserve this pipeline-bound sender rule.
- Pipeline AI, stage AI, and lead AI must be enabled.
- The organization needs available SHVYA AI credits and must not be blocked by
  Superadmin. These credits are separate from OpenAI API billing.
- The worker must have the working OpenAI key and the same DB/Redis configuration
  as web. Redis and the Celery worker must be running.

API engagement is queued immediately after the inbound transaction commits on
the dedicated `ai_realtime` queue, with no artificial countdown. Hosted WhatsApp
also requires the gateway and Celery Beat. Hosted debounce is capped at five
seconds (the existing processing-budget wakeup can start it sooner), and a
dedicated recovery scan runs every five seconds on `hosted_ai`. The normal hosted
delivery target is 30 seconds. Provider/network latency, queue capacity and
Account Health pauses can exceed that target; verify actual delivery timestamps.
New contacts require auto lead creation, a mapped pipeline with an active stage,
and eligibility under the existing-chat ignore rules. Existing leads can receive
AI replies without enabling auto lead creation. Historical sync does not trigger AI. A separate Hosted session reconciliation pass periodically repairs stale gateway/session connection state; this is distinct from the five-second AI job recovery scan.

Structured replies include Playbook qualification evidence and validated CRM actions as well as
the visible WhatsApp text. Older `.env` files set
`OPENAI_ENGAGEMENT_MAX_OUTPUT_TOKENS=300`, which can truncate this JSON envelope
as a conversation progresses. Structured engagement now enforces a minimum of
700 tokens, with 1400 for its single repair attempt, so existing deployments
benefit without editing `.env`. Other response schemas retain their own limits.
The prompt distinguishes the requirement before the inbound answer from the
next unresolved requirement after evidence extraction. Malformed JSON, invalid
evidence and an incorrect next-question ID all receive the same one bounded
repair using the original turn context; repaired output is fully revalidated.
Provider initialization failures release the turn's generation claim. Invalid
repairs still fail closed and never persist unsupported answers.

Inspect one affected lead on the VPS (IDs are available in CRM URLs):

```sh
docker compose exec -T web python manage.py diagnose_whatsapp_ai --organization-id ORG_UUID --lead-id LEAD_UUID
docker compose ps
docker compose exec -T worker celery -A config inspect ping
docker compose logs --since=10m ai_realtime_worker hosted_ai_worker worker beat
```

The diagnostic is read-only and does not call OpenAI or WhatsApp. It reports
configuration/permission blockers and the latest message/job status, without
printing keys or message contents. It does not prove that credentials work or
that workers/Beat are running. An overdue queued hosted job points to scheduler
or worker checks; a processing/failed job requires the corresponding worker log.
Keep provider logs private and redact credentials before sharing.

After deploying the fix, send a new message from a test contact. Verify it appears
as inbound, is attached to the intended lead/pipeline, and is followed by an
outbound message whose delivery status advances. Do this once for each transport;
allow the hosted delay. No live customer messages are sent by the automated tests.

Internal summaries and qualification notes run separately from customer replies.
Short conversations schedule a coalesced refresh after 20 seconds so they do not
remain empty waiting for six messages. CRM extraction uses the engagement call,
with organization attribute definitions and available pipeline stages supplied
explicitly. Free-form qualification answers retain their inbound evidence and
are persisted only after the worker checks conversation freshness. Repeated
generation tasks reuse a cached decision rather than purchasing the same reply
again; the outbound transaction still owns duplicate-send protection.


## Current lead-creation and conversation checks

For Hosted live inbound contacts that arrive with a WhatsApp LID instead of a usable phone number, confirm the gateway resolved the phone identity before expecting automatic CRM lead creation. A live unresolved LID must not create a fake phone-based lead. Historical contact/message sync remains non-live and must not trigger AI qualification.

For API/Coexistence, inspect the receiving account/business number and the lead's current pipeline together. An existing lead keeps its CRM pipeline unless a validated CRM transition explicitly changes it.

When a lead asks a substantive question while qualification is active, expected behavior is: answer that question from approved organization/knowledge evidence first, then continue with the next unanswered Playbook question. Repeating Q1 or jumping over the requested answer is a conversation-priority bug, not expected qualification behavior.

## Read-only diagnostic connector

Organizations can grant an API key the dedicated `can_read_diagnostics` permission and authorize the read-only SHVYA diagnostic connector. It can surface tenant-scoped diagnostic state without granting CRM mutation rights. OAuth/access tokens are hashed and access audit rows store request fingerprints/metadata rather than raw customer conversations, lead attributes or credentials.
