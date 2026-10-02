---
name: shvya-diagnostics
description: Perform read-only Shvya operational diagnosis across leads, conversations, AI, Workflows, integrations, runtime health and production traces.
---

# Shvya diagnostics

Use for investigation without applying repairs.

## Workflow

1. Verify actor, tenant, scope and time window.
2. Choose the smallest evidence path: `find_leads`/affected leads, lead snapshot, conversation, message trace, AI diagnostics, integration health, Workflow trace, recent errors, runtime health and production trace.
3. Correlate identifiers and timestamps rather than inferring causation from unrelated errors.
4. Use `trace.content.read` only when bounded rendered content is necessary; preserve redaction and never reconstruct secrets.
5. Distinguish configuration state, observed runtime behavior and unavailable evidence.
6. Identify the first verified failing layer and the domain owner for repair.

## Guardrails

Read-only means no stage movement, attribute repair, reconnect, resend, activation or configuration write. Never inflate a bounded sample into an organization-wide claim.

## Output

Return scope, evidence chain, PASS/FAIL/UNKNOWN by layer, first failure, confidence, affected IDs/counts when verified, and the recommended domain skill.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
