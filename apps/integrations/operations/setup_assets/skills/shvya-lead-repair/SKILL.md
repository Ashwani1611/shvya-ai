---
name: shvya-lead-repair
description: Diagnose and safely repair a specific Shvya lead's stage or non-sensitive attributes using persisted evidence and canonical CRM services.
---

# Shvya lead repair

Use for one or a bounded set of explicitly identified leads where CRM state is wrong or incomplete.

## Workflow

1. Confirm organization context and resolve the exact lead with diagnostic tools.
2. Read `get_lead_snapshot`, relevant conversation/trace evidence, qualification diagnosis and current pipeline/stage/attributes.
3. Determine whether the issue is lead state, qualification execution, automation, routing or a broader schema/configuration defect. Do not patch a lead to hide a systemic problem.
4. For non-sensitive attribute corrections, use `update_lead_attributes`. For a normal valid stage transition, use `move_lead_stage`. For completed qualification reconciliation, prefer `repair_qualification_stage`.
5. Dry-run each mutation, respect backend approval, then read back the exact lead.
6. If multiple leads are affected, first use diagnostics to prove a common root cause. Do not silently loop hidden writes across a large population when no bulk repair tool is exposed.

## Guardrails

Do not write credential-like values, bypass required attributes, force Qualified without qualification evidence, or cross pipelines by guessing a target. Preserve the newest explicit customer correction.

## Output

State the root cause, evidence, exact fields/stage repaired, audit reference, read-back result and whether a broader configuration fix is still required.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
