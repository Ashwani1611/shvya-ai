# Incident repair domain checks

## Evidence
Start from a bounded incident definition: organization, affected feature/resources, first/last observed time, customer impact and diagnostic root cause.

## Known traps
- Repairing symptoms on individual leads can hide a systemic runtime/config defect.
- Provider outage should not be compensated by weakening business rules.
- Ambiguous previous sends/writes must be read back before retry.
- Broad rollback can destroy unrelated correct changes.
- “Recovered now” is not the same as root cause fixed.
- A successful configuration write is not incident closure until the failing scenario is re-tested.

## Verification
Apply the smallest reversible fix at the producing layer, capture audit references, rerun the original scenario, quantify residual impact and record external dependencies/commitments.

## Handoffs
Use domain skill for the actual repair; this skill coordinates cross-domain recovery and closure.
