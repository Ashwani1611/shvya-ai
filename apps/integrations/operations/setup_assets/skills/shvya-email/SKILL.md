---
name: shvya-email
description: Design and diagnose Shvya email follow-up dependencies, sender readiness, Cadence/Workflow usage and delivery evidence.
---

# Shvya email

Use for email steps, sender readiness, recipient mapping and delivery failures.

## Workflow

1. Verify organization context, lead email availability, team/sender settings, relevant integration lifecycle/health, Cadence/Workflow configuration and recent errors.
2. Treat the lead's CRM email as the recipient source unless a live schema explicitly supports another destination.
3. Author email through supported Cadence or Workflow surfaces only. Preserve subject/body variables supported by that surface.
4. Check sender readiness before attributing a missing email to automation logic.
5. For an uncertain SMTP/provider outcome, inspect delivery evidence before retrying; at-most-once behavior is safer than an automatic duplicate.
6. If email integration lifecycle actions are available, use them only for the requested account/resource and preserve history on disconnect.
7. Re-run cadence/workflow simulation and inspect the resulting delivery record after repair.

## Guardrails

Do not invent arbitrary recipient addresses, SMTP credentials or unsupported attachments. Do not report queued as delivered.

## Output

Return recipient/sender resolution, automation path, delivery state, first failure and verified next action.
