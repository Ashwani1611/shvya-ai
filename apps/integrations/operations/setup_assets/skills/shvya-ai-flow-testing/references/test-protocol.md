# Production-engine test protocol

## Evidence levels

| Level | What it exercises | What it does not establish |
|---|---|---|
| Static audit | Source and configuration consistency | Actual runtime behavior. |
| Deterministic simulations | Compiled questions, mappings, rule predicates, schedule calculations | Generated language, provider calls, persisted end-to-end behavior unless expressly reported. |
| No-send production-engine fixture | Actual reply engine, controlled model calls and saved owned fixture state | Incoming webhook, transport worker, provider delivery or device receipt. |
| Authorized channel delivery | Real sender/provider and exact approved test recipient | Every other channel or future workload. |

Report each result at the level exercised. Never substitute demo chat for persistence tests or transport tests.

## Run manifest and budget

Create a run only through the dedicated server tool. Record organization, run ID, owned fixture IDs, pipeline/stage, channel context, config revision, model policy, created timestamp, maximum turns/provider calls/credits, audit receipt and budget usage. Inspect exact limits from live schema. Do not raise a server cap or use a new run to evade an agreed task budget.

Budget applies across all scenario runs and reruns in the authorized task, not only each individual API run. Maintain a task ledger with reserved and consumed units. Reserve 15% for diagnosis. A provider retry, schema repair or summary pass can count separately; use server usage rather than assuming two calls or fixed coins per turn. If pilot cost exceeds the estimate, resize remaining coverage and report it. A low-credit/failure condition is a stop, not permission to change a wallet.

## Baseline and state observations

Use a cooperative fresh lead first. Capture welcome once, first substantive answer handling, one next question, accepted letter/text/multiple-value answer, immediate or completed saved state, all-required qualification, final acknowledgment, correct destination and post-close response. Inspect actual completion/async markers before interpreting missing state. If unavailable, label timing/state observation insufficient.

Read the run after every significant transition and at the end. Keep response body, provider/model trace IDs, error classes, persisted attributes, summary, stage, qualified status and blocked actions where exposed. The real question is whether intended behavior occurred; passing prose with missing attributes fails a persistence requirement.

## Per-turn execution

1. Read the current transcript/state appropriate to the actor.
2. Generate the lead message using only persona/public information.
3. Assign a stable unique idempotency key for this exact run/turn/message.
4. Preview and apply with the current backend approval contract; do not reuse a receipt on different text.
5. Read result/status and budget; reconcile an uncertain outcome before retry.
6. Record evidence, expected predicate and whether later async completion is still pending.

No test turn calls send_message, sends a template, enrolls a real lead, modifies a company switch or triggers unrelated production automation. Server-owned fixtures are the only write targets. The dedicated cleanup tool enforces manifest ownership.

## Sampling and reruns

Prioritize blocking paths, then expected revenue paths, boundary conditions and polish. One scenario per distinct business behavior; add an adversarial variation only if it probes a different failure. For BLOCK reproduce at most three times in independent runs; for FIX at most twice, within cap. Keep nondeterministic failures visible as intermittent. Stop optional reruns once the remaining risk is resolved.

A human opt-out, source-fabrication, cross-tenant exposure or unintended real send is a blocking issue. Stop the affected test path immediately, preserve evidence and run fixture cleanup. Do not patch company configuration inside this testing skill.

## Cleanup

Call cleanup for every run ID in the task ledger. Confirm returned owned fixture cleanup state by read-back. Never derive cleanup from phone prefix, display name or an arbitrary imported fixture list. On cleanup failure, report exact remaining run/fixture IDs and status without trying ordinary lead bulk deletion. Retain audit evidence and do not expose unnecessary personal data.

## Current executable boundary

The native adapter currently accepts channel=whatsapp only. It executes reply generation, canonical CRM persistence and internal summary on server-owned isolated fixtures. It intentionally does not execute files, reminders, Calendar, Workflows or customer transport. Native Instagram fixture isolation is unavailable. Consequently a generated brochure promise can be judged for source correctness, but this harness cannot pass a file-delivered assertion; likewise reminder/booking execution remains UNVERIFIABLE here. Read actual tool state and report these exclusions prominently.
