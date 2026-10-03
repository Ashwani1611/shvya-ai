# AI engagement: action outcomes, Instagram, final replies and source operations

This follow-up to #558/#559/#561/#563 connects existing execution, transport,
grounding and ingestion boundaries. It does not install another AI engine, alter
Hosted 45-second pacing, enable the opt-in recovery feature, or send test messages.

## Live Playbook and action consistency

Action plans carry a fingerprint of authored organization instructions,
qualification rules, allowed actions and pipeline/stage configuration. The existing
CRM executor re-reads that policy before applying pending actions under its lead
lock. A policy change rejects a stale plan; an already committed receipt remains
idempotent. Knowledge additions and the executor's own dynamic attribute creation
are not interpreted as an authored-policy change.

Generation and independent grounding now see bounded source-bound AIActionReceipt
outcomes. Source identity is verified against the real tenant/account/lead (and
Instagram conversation); lead-wide markers cannot manufacture an execution result.
Attribute writes which change nothing are recorded as no-op. Reminder existence
and current status, current stage agreement and note/contact existence qualify
historical receipts. Private note bodies and contact values are not copied into
these outcomes. Missing receipts mean unconfirmed, not proven failure. A reminder
is not proof a human callback has been accepted. A late ordinary attribute or lead
source/pipeline change also invalidates the existing response state revision.

This preserves the existing authored Playbook parser, conditional qualification
and backend authorization. It is not a universal new natural-language rule compiler.
Configuration must still express a supported, unambiguous business policy.

## Instagram delivery outcomes

Instagram uses its own message rows, not WhatsApp file history. Final composition
reads file selection from the exact inbound source and transport from the same
organization/account/conversation/document. Clearing a selection keeps it cleared.
Queued rows cannot be elevated by task return strings. `sent` plus a provider ID
means provider acceptance; `read` requires the persisted read state and provider ID.
There is no invented `delivered` state. Claimed or inconsistent outcomes remain
unknown and are not automatically made safe to resend.

The send completion transaction refreshes the row after network I/O using the
webhook's conversation-first lock order. It preserves current source metadata,
read acknowledgement and first sent timestamp, including echo-before-response
races. Late failures cannot demote provider-accepted/read messages. Existing task
throttles, eligibility, signed webhooks and explicit-rejection retry ownership stay.

## Final-response correction and last-resort replies

The existing single wording correction now also runs during the language-only
post-effect pass. It changes text only, retains backend-selected effects/files,
and must pass independent grounding again. It is still constrained by existing
admission and provider limits, not a new retry loop or hard end-to-end deadline.

When verified wording cannot be produced, finite last-resort templates distinguish
technical retrieval trouble, an unverified detail, ambiguity, conflicting evidence
and an unconfirmed action. Pricing/policy uncertainty is specific without inventing
facts or claiming the business has no information. Mixed qualification/business
questions are not replaced by an unrelated qualification acknowledgement.

These deterministic templates cover English, Hindi and Hinglish; normal generation
and existing grounding continue to enforce the full Bot Languages/Playbook policy.
Arbitrary conditional-language instructions and other languages do not acquire a
new universal deterministic interpreter. Verify configured languages in live trials.
Do not treat a successful CI run as proof of naturalness or semantic accuracy.

## Explicit source repair

`evaluate_ai_recovery` readiness now includes inactive failed/unready documents and
candidate repair IDs. The new `repair_ai_knowledge` command links those diagnostics
to the existing ingestion/reindex tasks. It is an operator-host CLI, not a customer
message action, raw SQL tool or public endpoint.

First obtain a tenant-scoped metadata/content-state fingerprint without AI calls:

```sh
python manage.py repair_ai_knowledge --organization-id ORG_UUID --document-id DOCUMENT_ID
```

The result is a bounded plan with IDs/codes/counts, not original file bytes, private
content, URLs or provider errors. It detects metadata/content changes, not a lock
on remote URL content. Unsupported/empty/oversized sources require manual review.
Disabled URL sources, retired completed documents, superseded versions and
cross-tenant chunks cannot be automatically reactivated.

Explicitly apply the exact plan (this can consume normal organization AI credits):

```sh
python manage.py repair_ai_knowledge --organization-id ORG_UUID --document-id DOCUMENT_ID \
  --apply --expected-fingerprint FINGERPRINT_FROM_DRY_RUN
```

A durable KnowledgeRepairRequest deduplicates the tenant/document/revision and
publishes only on transaction commit. It dispatches the existing upload ingestion,
URL ingestion or missing-embedding reindex task. The task identity is stable,
claims are transactional, and existing bounded Celery retries remain the sole
provider retry mechanism. No provider call is held inside the repair claim lock.
The request does not become succeeded until the tenant's completed active document
and nonempty, fully embedded active index are verified. URL source eligibility is
checked again at publication so a late repair cannot resurrect a disabled source.

Inspect a request; redispatch only queued/broker-publication failures:

```sh
python manage.py repair_ai_knowledge --organization-id ORG_UUID --request-id REQUEST_UUID
python manage.py repair_ai_knowledge --organization-id ORG_UUID --request-id REQUEST_UUID --dispatch
```

Broker uncertainty can create duplicate deliveries of the same task ID; the claim
suppresses duplicate execution. A crashed running request is intentionally not
blindly replayed. Inspect canonical document/task state before operator recovery.
This release does not add periodic repair-all, reopen retired content or silently
spend credits from customer turns. Migration 0020 must precede application/worker
startup; use the existing deployment migration gates.

## Actual evaluation credit reporting

The no-send OFF/ON evaluation retains its existing explicit `--live` entry point.
A scoped observer records reservation identities created by each synchronous test
turn. Reports join only those identities with that tenant's actual reservation and
AI usage ledger; no wallet-before/after subtraction and no unrelated traffic.

Per-turn reports include settled AI credits, input/output tokens, pending reserved
credits, released reservation counts and completeness. Missing/duplicate/inconsistent
ledger rows or database failures are not reported as complete zero cost. Failed
turns retain observed usage without becoming passing comparisons. Nested observers
are supported and the context resets on every exit. Observation changes neither
credit prices nor billing/settlement/refund behavior.

The report's `usage` section aggregates baseline and recovery credits and supplies
`incremental_credits` only for comparable completed accounting. Units are SHVYA AI
credits; `currency_cost` is null, not an invented provider or INR price. Limits are
bounded turn-count/time admission, not a hard total token/currency spending cap.

## Validation and rollout

Run the contract, new database/runtime tests, existing AI/channel suites, migration
check and repository CI/security gates. Tests mock external networks, use isolated
test organizations and do not prove real recipient delivery. These changes leave
recovery enable/allow-list settings unchanged. Runtime readiness, a reviewed live
OFF/ON comparison and controlled test-recipient checks still precede customer
activation. No real repair, live model benchmark, human handoff or customer send is
implied by adding the services or by passing automated tests.
