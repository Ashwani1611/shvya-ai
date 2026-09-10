# Diagnosing WhatsApp AI replies

Docker images now default to `config.settings.prod`. If the VPS `.env` explicitly
sets `DJANGO_SETTINGS_MODULE`, use `config.settings.prod` there too. Recreate web,
worker, and beat after changing credentials or settings; restarting a container
alone does not load a changed Compose `env_file`.

The API webhook routes by the business phone number, not Meta's opaque
`phone_number_id`. Pipeline country code and number are normalized before matching.
Existing leads retain their CRM pipeline. Leads previously created in the fallback
pipeline must be reviewed and moved to the intended pipeline through the CRM.

Required for both transports:
- The pipeline's WhatsApp number must match the connected account.
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
AI replies without enabling auto lead creation. Historical sync does not trigger AI.

Structured replies include qualification evidence and CRM actions as well as
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
