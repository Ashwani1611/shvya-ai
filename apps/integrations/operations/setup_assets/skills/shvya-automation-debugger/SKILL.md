---
name: shvya-automation-debugger
description: Diagnose failed, duplicated, skipped or conflicting Shvya Workflow/Cadence automation and identify the exact producing layer before repair.
---

# Shvya automation debugger

Use when a Workflow did not trigger, a Cadence did not enroll/fire, an action repeated, outreach continued after handoff/opt-out, or automation changed the wrong CRM state.

## Diagnostic workflow

1. Confirm organization, lead, event time and expected behavior.
2. Read lead snapshot, Workflow/Cadence configuration, messaging settings and `get_workflow_trace`.
3. Verify trigger eligibility, scopes, condition values, event payload, resource IDs and enabled state.
4. For Cadence issues, verify enrollment source, sender/provider, step order, schedule, template approval, business hours and suppression state.
5. Inspect recent errors/runtime health when the configuration matched but execution did not occur.
6. Distinguish configuration failure, event-generation failure, queue/worker failure, provider failure and deliberate suppression.
7. Hand the repair to `shvya-workflow-builder`, `shvya-cadence-builder`, `shvya-channel-routing`, CRM/qualification, or incident repair as appropriate.
8. Re-run the same simulation/trace after repair.

## Guardrails

Never compensate for a missing event by creating a broader trigger without evidence. Never remove opt-out/handoff suppression to make tests pass. Do not replay uncertain outbound sends automatically.

## Output

Return the expected path, observed path, first divergence, evidence, repair owner, and verification result.
