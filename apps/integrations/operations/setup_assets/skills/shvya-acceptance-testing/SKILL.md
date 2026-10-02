---
name: shvya-acceptance-testing
description: Run Shvya configuration and behavior acceptance checks across qualification, AI policy, knowledge, CRM actions, automation, routing, handoff and delivery readiness.
---

# Shvya acceptance testing

Use as the final gate after setup/repair and before claiming a configuration works.

## Test layers

Keep three evidence levels separate:

1. Structural validation: schemas, dependencies, routing, configuration integrity and required provider state.
2. Deterministic simulation: qualification, AI policy, Workflow and Cadence simulations plus `run_acceptance_suite`.
3. Real authorized behavior: representative channel conversation/provider delivery tests when explicitly authorized and supported.

## Workflow

1. Record the intended business acceptance criteria and exact organization.
2. Run `validate_organization_configuration`, qualification validation, Workflow validation/simulation, Cadence simulation/batch validation, routing checks and Calendar validation as relevant.
3. Run `test_ai_response_policy`, `simulate_ai_conversation` and `run_acceptance_suite`.
4. Cover positive and negative scenarios: qualification branches/corrections, known vs unknown facts, multilingual behavior, refusal, pricing/policy, opt-out, human handoff, stage/attribute/reminder effects, Workflow overlap, Cadence exits, routing and provider readiness.
5. When real testing is authorized, record actual provider acceptance/delivery separately from simulated readiness.
6. Failed cases go to the owning domain skill; rerun only the affected suite plus required regressions.

## Verdicts

Use only: READY_BY_DETERMINISTIC_CHECKS, READY_WITH_PROVIDER_EVIDENCE, PARTIALLY_READY, or BLOCKED. Never call a simulated send delivered or a saved configuration production-proven.

## Output

Return scenario matrix, evidence level, pass/fail, unresolved gaps, owner and final bounded verdict.
