---
name: shvya-qualification
description: Build, validate, test and repair Shvya qualification questions, mappings, criteria and completion-stage behavior.
---

# Shvya qualification

Own the authoritative qualification contract rather than treating qualification as prompt-only behavior.

## Workflow

1. Verify context. Read `get_qualification_configuration`, `get_ai_configuration`, CRM stages/attributes and the affected lead snapshot when troubleshooting.
2. Define the required questions, exact accepted values, conditional eligibility, mappings, completion criteria, target stage and final acknowledgement.
3. Preserve one-question-at-a-time behavior, prior answered values, explicit corrections, option-letter context, user questions mid-flow, refusal handling and human handoff.
4. Bind mappings only to real tenant-owned attributes and valid options. Bind the completion target to a real stage in the intended pipeline.
5. Run `validate_qualification_configuration` and `simulate_ai_conversation` before applying.
6. Use `upsert_qualification_configuration` with dry-run/approval/read-back. Review the AI Playbook afterward because structured qualification updates can replace qualification-owned sections.
7. For a lead that completed correctly but did not move, use `diagnose_lead_qualification` and, only when backend evidence proves completion, `repair_qualification_stage`.

## Required test matrix

Test letter answers, full text, yes/no where applicable, mixed-language answers, corrections, multiple inbound messages, user questions between qualification answers, pre-existing CRM values, refusal, completion, attribute persistence and stage movement.

## Output

Return the normalized question contract, mappings, target, simulations run, persisted changes and any lead-level repair evidence.
