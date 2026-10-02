# AI Brain evidence coverage and recovery — opt-in first release

This release strengthens the existing Python LangGraph decision flow. It does
not add LangGraphJS, a new AI engine, checkpoints, migrations, transport changes,
or a second implementation of CRM actions.

## Flow

`prepare -> deterministic_extract -> route -> optional retrieval -> coverage`

Coverage routes to generation or **one** targeted retrieval retry, followed by
one reassessment. Generation still passes through backend validation and the
existing independent grounding gate. The graph has no recovery cycle.

The assessor checks each requested factual detail against identified approved
sources, not just search similarity. It proposes a bounded search query using
recent context (including Hindi/Hinglish) when evidence is partial/insufficient.
Assistant messages can resolve the topic, never establish company facts. Its
verdict is advisory and cannot authorize CRM writes, files or unsupported claims.
Malformed verdicts and invented source identifiers fail closed to the existing
generation/grounding path. Ambiguous or conflicting evidence does not trigger
speculative repeated searches. Existing Playbook, source, language, qualification,
tenant and post-commit response controls are preserved.

## Retrieval outcomes

The opt-in graph path calls the existing metered `EmbeddingService` and canonical
`KnowledgeRetrievalService.retrieve_hybrid`; it does not reimplement ranking.
It separates `no_match` from `no_documents`, `source_not_ready`, `source_failed`,
`index_empty`, `storage_error`, `retrieval_error`, `embedding_error`, `timeout`,
and `budget_exhausted`. Source health checks are tenant-scoped and do not call
an embedding provider when no searchable chunks exist. Keyword retrieval can
still succeed when embedding fails; the degradation is separately recorded.
A failed or empty search is never proof that the business has no answer.

Recovery does not send messages or execute actions. Existing file candidates are
recomputed after evidence changes. Django remains authoritative for business
state; source/lead/flow snapshots are not replaced during recovery. Post-commit
language-only passes skip the extra evidence assessment.

## Rollout and rollback

Off by default. BOTH of these environment variables are required:

```env
AI_BRAIN_RECOVERY_ENABLED=1
AI_BRAIN_RECOVERY_ORGANIZATION_IDS=<internal-test-organization-uuid>
AI_BRAIN_RECOVERY_BUDGET_SECONDS=20
```

Use comma-separated organization UUIDs to extend the explicit allow-list. An
empty list does not mean all tenants. There is no wildcard rollout. Set the
switch to `0` and restart the affected application workers to restore the
previous path. No organization, environment or deployment setting is changed
by this source patch.

The optional recovery budget is measured from the start of this graph invocation
and clamped to 5–30 seconds. No new recovery provider call starts with less than
five seconds remaining. Coverage and query embedding calls use five-second
network timeouts with SDK retries disabled. At most one extra retrieval and one
reassessment are possible; the existing grounding repair remains separately
bounded by its current policy. This is not a hard end-to-end latency guarantee:
Django/database work, provider transport details and outer execution/retries
remain governed by their existing limits. Observe total turn latency in canary
trials before broader activation.

## Diagnostics and tests

`knowledge_recovery.events` records bounded outcomes, attempt counts, candidate
counts and supporting source identifiers through the existing tenant-scoped
trace buffer. New events do not include customer questions, rewritten queries,
source text, private notes or exception messages. A trace failure cannot block
a reply.

Credential-free contracts:

```sh
python -m unittest apps.ai_engagement.tests.test_evidence_recovery_contract -v
```

Django/LangGraph integration and tenant-isolation tests (no live AI calls):

```sh
pytest apps/ai_engagement/tests/test_evidence_recovery_contract.py \
       apps/ai_engagement/tests/test_evidence_recovery_integration.py
```

Before activating, compare the same anonymized conversations, model and knowledge
snapshot on old and new paths. Include uploaded-file-only facts, Hindi/Hinglish,
short contextual replies, mixed qualification/questions, conflicting sources,
genuine missing facts and degraded retrieval. Track correctness, unnecessary
unknown replies, qualification repetition, latency and credit usage. Channel
send/delivery tests remain necessary: unit/graph tests do not prove that a real
WhatsApp or Instagram recipient received anything.

## Deliberately outside this first release

No action/delivery lifecycle rewrite, automatic re-indexing, durable checkpoint
migration, blanket fallback removal, or production activation. Existing guards
must not be bypassed merely to avoid an unknown response. The package's CI and
live canary outcomes must be reported separately from local contract-test results.
