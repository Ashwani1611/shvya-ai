# Automation debugger domain checks

## Evidence path
Expected event → event generation → Workflow eligibility → conditions → action execution → Cadence enrollment/step eligibility → queue/worker → provider/delivery. Include current and incident-time config when available.

## Known traps
- Client symptom can name the wrong layer; trace the actual producing mechanism.
- Enabled rule with zero executions differs from disabled rule and from missing rule.
- A lead can be correctly moved/stopped by AI while an independent Cadence keeps messaging.
- Retry attempts can look like duplicate automation; check idempotency keys and distinct side effects.
- Business hours/suppression/handoff can intentionally skip execution.
- Provider delivery failures should not be “fixed” by broadening Workflow conditions.

## Verification
Classify as never matched, matched/action failed, intentionally skipped, duplicate/idempotency failure, downstream delivery failure, or configuration invalid. Re-run exact simulation/trace after repair.

## Handoffs
Workflow definition → Workflow builder; Cadence → Cadence builder; routing/provider → channel skill; runtime outage → incident repair.
