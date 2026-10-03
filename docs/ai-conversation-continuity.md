# Unanswered-turn continuity without changing Hosted pacing

## Scope

This change extends the existing shared engagement runtime and canonical
LangGraph. It does not create another engine, queue, sender, state table, or
retry owner. Hosted's 45-second account-level AI send gate is unchanged. This
patch does not alter API/coexistence pacing either; it makes no deployment,
credential, recovery-activation, organization, or channel-setting changes.

## Corrected paths

- Preserve a bounded unanswered inbound burst in retrieval and provider input.
  A product/pricing request must not disappear from the search merely because
  the customer subsequently asks for the brochure repeatedly.
- A short nudge may leave a previous request outstanding. Existing request
  classification and file eligibility are reused; a pending explicit brochure
  request followed by "please" can still expose its eligible candidate.
- A validated qualification answer plus an independent information request
  uses response composition/retrieval instead of a question-only shortcut.
  The current inbound ID and deterministic qualification state are unchanged.
- Reject a repeated long, matching English AI-assistant onboarding introduction
  through the existing validation/repair path. Brief greetings, repeated
  approved answers, and failed/unsent introductions are not blocked by this rule.
- Model-authored silence remains rejected by the existing backend policy,
  including after qualification. No AI OFF, opt-out, human-lock, source/stage,
  pipeline routing, or transport control is overridden.

## Bounds and authority

Only the already tenant-scoped context is read. At most 100 recent messages,
12 distinct pending texts, 2,400 additional customer-content characters, and a
900-character retrieval query are considered. Context-only compaction retains
the newest real ID for duplicate wording; it does NOT mark events processed,
deduplicate provider events, or authorize a new send. The current inbound
message is never replaced by an earlier request for qualification/execution.

The file-candidate nudge path performs at most one additional lookup using the
existing candidate service, with the same organization, lead, stage and rules.
The original current context and grounding/action checks remain authoritative.
A cancellation/topic change does not trigger this additional nudge lookup.
There is no new provider call or automatic file resend in this layer.

A sent/delivered/read outbound is a conservative context boundary. Missing
status is also a boundary for legacy/preview compatibility. Explicitly queued,
sending or failed outbounds cannot erase an unanswered burst. **A context
boundary is not proof of semantic answer coverage or recipient delivery.** This
is not a durable, cross-reply request ledger; partially answered requests after
an intervening sent reply still depend on existing conversation/receipt state.

The runtime is installed after the existing qualification/grounding layers and
before the canonical graph is recompiled. Its hooks wrap, rather than replace,
the existing retrieval, validation, policy and candidate ownership.

## Validation

Credential-free tests (no Django, broker, provider, customer messages or sends):

```sh
python -m unittest apps.ai_engagement.tests.test_conversation_continuity_contract -v
```

Installed-runtime tests and affected suites in the normal Django test environment:

```sh
pytest apps/ai_engagement/tests/test_conversation_continuity_contract.py \
       apps/ai_engagement/tests/test_conversation_continuity_integration.py
pytest apps/ai_engagement/tests apps/channels/tests
```

Use an internal synthetic conversation: qualification answers, completion,
product/pricing/package question, repeated brochure requests, then "please".
Verify the product answer, source-bound action records and actual controlled
recipient delivery separately on API, coexistence, Hosted and Instagram.
Sandbox selection/preview does not prove delivery or persisted CRM writes.

## Incident validation still required

The supplied symptom does not identify its production lead/account or execution
records. This patch does not claim to diagnose or repair an unhealthy worker,
blocked queue, exhausted wallet, disabled destination stage, disconnected
session or unknown send outcome. Do not silently enable controls, replay old
customer requests or mark an uncertain file attempt as delivered.

Use the existing `check_ai_runtime` command for current consumers and recent
aggregate failures (its default window is 90 minutes), then inspect the actual
lead's inbound -> job/decision -> outbound -> provider receipt chain. Preserve
45-second pacing and the existing bounded transport recovery. Live evidence
and CI are separate release requirements; do not infer successful delivery
from a passing test or an inbox bubble.
