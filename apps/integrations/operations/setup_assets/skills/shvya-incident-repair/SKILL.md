---
name: shvya-incident-repair
description: Coordinate bounded Shvya production incident repair from diagnosis through the smallest safe configuration/state fix and post-change verification.
---

# Shvya incident repair

Use only after an actual operational incident has a defined organization, impact and desired recovery state.

## Workflow

1. Start with `shvya-diagnostics` evidence. Establish affected feature, leads/accounts, time window and first failing layer.
2. Classify the incident: CRM/qualification, AI/knowledge, Workflow/Cadence, channel routing/provider, Calendar/integration, or runtime/worker infrastructure.
3. Prefer the narrowest reversible repair through the owning domain skill. Do not make broad configuration changes to compensate for an infrastructure outage.
4. For state repair, dry-run and obtain approval when required. For ambiguous previous writes/sends, read back/provider-check before retry.
5. Record operational commitments with `upsert_commitment` when recovery depends on an external approval, provider action or later acceptance step.
6. Run the original failing scenario or equivalent deterministic validation again.
7. Report residual risk and whether the incident is mitigated, fixed and verified, or still blocked.

## Guardrails

Do not delete evidence, disable consent controls, broaden routing or resend uncertain outbound messages as a shortcut. Infrastructure/server changes outside exposed MCP tools remain an explicit operator task.

## Output

Return incident scope, root cause, repair, audit references, verification, residual impact and follow-up commitments.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
