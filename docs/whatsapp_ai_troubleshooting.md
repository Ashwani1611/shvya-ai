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

Hosted WhatsApp also requires the gateway and Celery Beat. Its durable AI job
becomes due around 60 seconds after the latest inbound message and is dispatched
by the follow-up scheduler every 10 seconds. Account Health can defer delivery.
New contacts require auto lead creation, a mapped pipeline with an active stage,
and eligibility under the existing-chat ignore rules. Existing leads can receive
AI replies without enabling auto lead creation. Historical sync does not trigger AI.

Inspect one affected lead on the VPS (IDs are available in CRM URLs):

```sh
docker compose exec -T web python manage.py diagnose_whatsapp_ai --organization-id ORG_UUID --lead-id LEAD_UUID
docker compose ps
docker compose exec -T worker celery -A config inspect ping
docker compose logs --since=10m worker beat
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
