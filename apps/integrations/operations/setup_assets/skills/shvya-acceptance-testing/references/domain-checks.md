# Acceptance testing domain checks

## Evidence levels
Keep structural validation, deterministic simulation and real provider/customer-path evidence separate. A higher level can strengthen a lower one but never rewrite what was actually tested.

## Known traps
- Passing schema validation is not passing behavior.
- Sandbox/model simulation is not provider delivery.
- One happy-path test misses corrections, refusals, handoff, opt-out and unknown-fact behavior.
- A message delivered is not proof CRM side effects happened.
- A CRM side effect happening is not proof the customer-facing message was correct.
- Current provider readiness does not prove historical delivery.
- Test data must be isolated from production customer records unless an explicitly authorized representative case is used.

## Minimum matrix
Where relevant test: positive qualification, invalid/ambiguous answer, correction, customer question mid-flow, known fact, unknown fact, multilingual turn, human handoff, opt-out, stage/attribute/reminder action, Workflow match/non-match, Cadence timing/exit, routing, file sharing and provider readiness.

## Verification
Run every applicable scenario at its stated evidence level, preserve pass/fail per subsystem, and rerun failed cases plus affected regressions after repair. Never promote the final readiness verdict beyond the strongest evidence actually observed.

## Verdict discipline
Use only READY_BY_DETERMINISTIC_CHECKS, READY_WITH_PROVIDER_EVIDENCE, PARTIALLY_READY or BLOCKED. Name exactly which evidence level each scenario reached.

## Handoffs
Failures return to the owning skill; rerun the failed scenario plus affected regressions after repair.
