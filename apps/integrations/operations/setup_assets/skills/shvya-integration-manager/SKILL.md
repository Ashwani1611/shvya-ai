---
name: shvya-integration-manager
description: Inspect and safely manage supported Shvya integration lifecycle state, readiness, disconnection and cross-feature dependencies.
---

# Shvya integration manager

Use for integration inventory, readiness, provider-state diagnosis or explicit disconnection.

## Workflow

1. Verify organization context and call `get_capability_discovery` when unsure which integration lifecycle actions are currently exposed.
2. Read `get_integration_lifecycle`, integration health and dependent routing/Calendar/team settings.
3. Separate connected, authenticated, subscribed, healthy and operational states; they are not equivalent.
4. Use provider-specific skills for WhatsApp, Instagram, email and Calendar details.
5. Use `disconnect_integration` only when the user explicitly intends disconnection of the exact supported integration/resource. Dry-run, review downstream effects, apply with approval and verify credentials are cleared while history is preserved.
6. If connect/reconnect is not exposed for a provider, create an operational commitment rather than inventing a tool or bypassing MCP with secrets.
7. Verify dependent Workflows/Cadences/routing do not remain falsely marked ready after lifecycle changes.

## Output

Return integration inventory, lifecycle state, dependency impact, supported actions, applied changes and blocked external/provider steps.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
