---
name: shvya-instagram
description: Review and diagnose Shvya Instagram professional messaging connection, inbound DM lead behavior, AI engagement and channel-specific delivery constraints.
---

# Shvya Instagram

Use for Instagram connection status, OAuth/webhook health, DM lead behavior, AI engagement, username/name capture and Instagram-specific file/message handling.

## Workflow

1. Verify organization context and inspect `get_integration_lifecycle`, `get_integration_health`, recent errors and relevant lead/conversation traces.
2. Distinguish OAuth authorization success, account binding, webhook subscription, inbound event receipt, lead creation/resolution, AI execution and outbound Send API delivery.
3. Instagram leads do not require a phone number. Preserve the platform identity/username and map a phone only when the lead explicitly supplies a valid number.
4. Check source/channel-specific AI Playbook instructions without replacing global behavior unnecessarily.
5. For connection failures, identify whether the failure is SHVYA callback/state validation, Meta permission/review, subscription, token/account state or webhook delivery.
6. Use lifecycle actions only when they are actually exposed. Current generic lifecycle tools may inspect/disconnect Instagram; do not invent a connect/reconnect tool if the live catalog does not expose one.
7. After repair, verify with an authorized representative DM flow rather than configuration state alone.

## Guardrails

Never request or expose Meta access tokens. Do not require phone for Instagram lead creation. Do not claim App Review or Meta account approval can be completed by MCP.

## Output

Return the connection chain, first failed layer, lead/AI behavior findings, supported repair path and end-to-end checks still required.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
