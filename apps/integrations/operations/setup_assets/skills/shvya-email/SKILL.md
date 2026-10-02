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

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
