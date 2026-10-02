# Execution and recovery contract

## Normal path

1. Resolve actor, tenant and task mode.
2. Discover current tool schema/capabilities when needed.
3. Read authoritative current state.
4. Build dependency-aware proposal.
5. Validate and simulate where supported.
6. Dry-run mutation.
7. Obtain approval only when the returned proposal requires it.
8. Apply exactly that proposal.
9. Read back authoritative state.
10. Run the smallest representative behavioral/acceptance check.
11. Report evidence and remaining gaps.

## Failure classes

### Validation failure
Do not weaken validation. Correct inputs/dependencies or hand off to the owning skill.

### Permission/capability denial
Do not bypass with another identity, REST, SQL, browser automation or secrets. Report the exact missing live capability/scope/policy requirement.

### Stale state
Re-read, rebuild the diff and obtain a fresh dry-run/approval.

### Ambiguous write timeout
Read back before retrying. If the mutation cannot be proven absent, stop and report NEEDS_REVIEW.

### Provider uncertainty
Inspect provider/delivery evidence before retry. Avoid duplicate sends, bookings or external actions.

### Runtime outage
Do not rewrite business configuration to compensate for an unavailable queue, worker or provider. Route to diagnostics/incident repair and preserve intended config.

### Partial cross-domain success
Keep verified independent changes, stop the blocked dependency chain, and report PARTIALLY_APPLIED with exact remaining work. Do not roll back unrelated correct state merely to make the report look atomic.

## Recovery discipline

Use stable returned IDs. Prefer reversible archive/disable over destructive delete. Preserve audit evidence. After recovery, rerun the exact failed scenario or the closest deterministic equivalent.
