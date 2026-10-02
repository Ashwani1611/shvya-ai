# Diagnostics domain checks

## Evidence discipline
Define organization, resource, time window and expected behavior. Use the smallest chain of lead/conversation/message/AI/integration/Workflow/runtime/production-trace evidence needed.

## Known traps
- Current config can hide incident-time state.
- Retry/trace counts can inflate distinct customer impact.
- One error near the timestamp is not automatically causal.
- Missing trace content due to scope/redaction is UNKNOWN, not proof the event did not occur.
- UI error text can be a downstream symptom.
- Different tools may expose summaries of the same underlying event; do not count them as independent evidence.
- Read-only diagnosis must not mutate “just to test”.

## Verification
Correlate identifiers/timestamps across layers and name the first verified divergence. State confidence and alternate explanations eliminated. Preserve read-only boundary.

## Handoffs
Return the owning domain skill and exact next bounded diagnostic or repair step.
