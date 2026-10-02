# Email domain checks

## Evidence
Read recipient CRM email, sender/team configuration, integration lifecycle/health, Workflow/Cadence step, variables/content, queue/delivery status and recent provider errors.

## Known traps
- Lead email missing/invalid is not a sender outage.
- Queued/sending is not delivered.
- SMTP/provider uncertainty must not be auto-retried blindly.
- Stable Message-ID/idempotency matters for duplicate prevention.
- A Cadence step can be valid while sender readiness is false.
- Reply-to/sender identity and customer recipient are separate fields.
- Email channel availability should not be inferred from a WhatsApp-enabled account.

## Verification
Resolve sender+recipient, validate automation/timing, inspect at-most-once delivery claim/provider outcome and run read-back after configuration changes.

## Handoffs
Cadence timing → Cadence builder; integration lifecycle → integration manager; CRM email data → lead repair.
