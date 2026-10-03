# AI engagement: applied actions, channel outcomes, recovery and cost

This release extends the existing Python/Django engagement services. It does not
add an AI engine, channel sender, automatic customer rollout, or alternative retry
owner. The CRM, canonical transport rows and immutable credit ledger remain the
sources of truth.

## Playbook and CRM actions

Explicit acquisition-source predicates are resolved independently of the current
chat channel. The existing conservative source parser is reused for mapping,
reminder, routing and guided-file applicability. Explicit channel predicates use
the actual conversation channel. Unsupported or ambiguous scopes do not authorize
an action. The unchanged qualification, semantic condition and tenant validators
still decide whether an applicable action is allowed.

Generated decisions carry a hash of the authored Playbook, Bot Languages and AI
enablement. The live transactional boundary compares that revision before effects
and final response persistence; changed policy requires regeneration rather than
executing an old decision. Legacy deterministic callers without the revision
remain subject to the current executor authorization contract. This is not a
snapshot lock over every document, stage or organization setting.

Only actual `executed`/`partial` results populate applied action types. An optional
attribute creation which writes no key is `not_applied`, not a completed update.
Current-turn confirmations read tenant-scoped `AIActionReceipt` results bound to
the persisted inbound message, not cached proposals or another turn's runtime
markers. Existing pending reminders may inform a response but do not prove a new
callback has just been scheduled. Receipt projections exclude note text, contact
handles and attribute values.

## Instagram outcome parity

Instagram retains its own message/account/conversation model and existing task
routing. Outbound file state is matched to the exact organization, lead,
conversation, account, inbound message, selected document and document version.
Queued, pending selection, provider-accepted, read, failed and uncertain are
separate outcomes. Instagram has no synthetic `delivered` state: `sent` means
provider acceptance, not proof of recipient delivery.

A durable submission marker is written before provider I/O. Duplicate/stale task
objects reload canonical state before sending. A confirmed sent/read message is
not submitted again; an uncertain submission is not blindly replayed. Explicit
provider rejection can be retried by the existing retry owner, while timeout or
unknown post-submission outcome requires reconciliation. Read/echo callbacks do
not regress read acknowledgement. Source-bound history is bounded and never
updates WhatsApp file history or overwrites a later lead turn.

Projection errors after provider execution cannot turn successful sending into a
new send attempt. Read-through canonical state remains available for diagnosis.
This does not implement a new manual resend or reconciliation UI.

## Final responses and fallbacks

Final language-only composition may use the existing single bounded wording
correction and independent revalidation. It cannot propose a new action or change
the approved file choice. Failed final validation is explicitly carried in the
backend decision so downstream file-freezing cannot revive a rejected selection.

Question-level evidence coverage is supplied to grounding. A generic unknown
reply with sufficient/partial evidence can be corrected; unrelated context alone
does not trigger this recovery when coverage explicitly says insufficient.
Unsupported answers are never approved just to remove a fallback. Mixed customer
questions and qualification answers do not collapse into acknowledgement-only
failure wording.

Provider-free failure copy distinguishes unverified details, technical failure,
conflict, clarification and unconfirmed actions. Detailed catalogs cover English,
Hindi and Hinglish; neutral uncertainty is supplied for the additional cataloged
languages. This is not a replacement for the existing Playbook-aware multilingual
grounding validator or a promise of universal language support. Unsupported
languages retain the safe default; no translation provider is called during an
outage. Authored supported answers, silence, opt-out and pause policies remain.

Recovery remains opt-in as described in `ai-brain-evidence-recovery.md`.

## Controlled knowledge repair

Apply the migration before using the operator command. `AIKnowledgeRepair` is a
durable receipt for one reviewed source repair, not a new ingestion pipeline.

Read-only planning (no AI calls and no repair writes):

```sh
python manage.py repair_ai_sources --organization-id ORG_UUID --document-id DOCUMENT_ID
```

Review `action`, `repairable`, `reason`, source version and the opaque fingerprint.
Only then authorize the exact metered operation:

```sh
python manage.py repair_ai_sources --organization-id ORG_UUID --document-id DOCUMENT_ID \
  --apply --expected-fingerprint FINGERPRINT_FROM_DRY_RUN --allow-credits
```

The command rechecks the plan under organization/document locks and schedules the
existing canonical upload, URL ingestion or missing-embedding reindex task after
commit. One active repair ticket per organization/document prevents duplicate
operator repair work; task identity and attempt checks suppress duplicate delivery.
Canonical Celery retries retain the same ticket and exact document/version.
Existing valid embeddings are not regenerated by missing-embedding repair.
Inactive URL sources, superseded versions and unsupported/no-identity repairs are
blocked. A completed-but-empty source requires explicit re-upload/review; this
command does not guess missing file bytes or republish deliberately hidden content.
Successful indexing is verified against stored tenant-scoped chunks. Ordinary
legacy ingestion continues using its existing recovery and publication controls.
This is not a claim of exactly-once execution across all legacy ingestion paths.

Inspect results and cost without another model call:

```sh
python manage.py repair_ai_sources --organization-id ORG_UUID --request-id REPAIR_UUID
```

A broker dispatch failure can be retried with `--redispatch --allow-credits` on the
same queued/dispatch-failed ticket. Running/uncertain requests cannot be blindly
redispatched. `--reconcile` checks already persisted completion without provider
calls; incomplete/unknown work remains explicit. Reconciliation after lost outcome
accounting marks cost incomplete rather than inventing zero usage. Keep canonical
provider/task logs and configuration access within the normal operator boundary.
No shell, secret, filesystem or mutation capability is added to read-only MCP.

## Exact cost reporting

The no-send `evaluate_ai_recovery` comparison reports baseline/recovery usage and
incremental internal AI credits. Context-local capture records reservation IDs
created by that operation and reads their tenant-scoped immutable usage ledger.
Other concurrent messages, top-ups or balance changes are not attributed to it.
Source repair reports use the same mechanism across canonical retry attempts.
No billing rates or settlement behavior is changed.

A settled charge is counted only with its matching ledger amount and token totals.
Released reservations contribute zero settled credits; pending, missing,
inconsistent, storage-unavailable or truncated accounting returns `total_credits:
null`. Confirmed settled usage remains separate. Capture is bounded and context
is restored even after exceptions. Reports contain counts/credits, never prompts,
answers, raw source text, credentials or reservation identifiers.

Units are **internal AI credits**, not rupees or a provider invoice. No currency
conversion is configured, so currency cost is explicitly unavailable. This is
measurement, not a hard cost or latency cap. Literal scenario checks do not prove
semantic answer correctness; controlled human conversation review is still needed.

## Verification and release gates

Credential-free contracts:

```sh
python -m unittest apps.ai_engagement.tests.test_engagement_consistency_contract -v
```

Django/runtime tests, all with mocked provider transport:

```sh
pytest apps/ai_engagement/tests/test_live_action_consistency.py \
  apps/channels/tests/test_instagram_delivery_outcomes.py \
  apps/ai_engagement/tests/test_source_repair_integration.py \
  apps/ai_engagement/tests/test_usage_attribution_integration.py
pytest apps/ai_engagement/tests apps/channels/tests
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
```

Tests must pass before merge. CI outcomes, operator live comparison and actual
recipient delivery are separate verification stages. This source change does not
activate recovery for an organization, repair production sources, change customer
Playbooks, spend live customer credits or send test messages. Verify relevant
workers' effective configuration and use controlled recipients before broader
rollout. Persistent LangGraph checkpoints remain deferred.
