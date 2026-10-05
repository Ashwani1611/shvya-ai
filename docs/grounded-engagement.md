# Grounded engagement and CRM summaries

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.

The existing inbound WhatsApp permission, debounce and deduplication flow invokes
one compiled LangGraph workflow:

`prepare → deterministic extraction → route → retrieve (when needed) → generate → validate actions → validate grounding → return`

Retrieval remains organization-scoped through `KnowledgeRetrievalService` and
pgvector. The evidence selector rejects invalid/non-finite scores, applies
`AI_RAG_MIN_SIMILARITY` (default 0.38), sorts strongest first, deduplicates content,
and limits the model's retrieved text to 12,000 characters. Retrieval preserves
the original turn's conversation snapshot. Authorized document candidates reuse
that retrieval pass.

The final grounding node independently checks generated replies against company
facts, retrieved evidence, inbound customer facts, and the runtime policy. A
negative or malformed verdict raises `EngagementError` before callers can send
the reply or execute its proposed actions. Provider errors propagate through the
existing task failure/retry handling. Deterministic questions and non-engagement
decisions do not require another model call. Generated replies incur one extra
provider call, using the engagement model and existing credit accounting. This
reduces unsupported replies but is not a guarantee of model correctness.

Conversation summaries use only selected messages since lead creation, excluding
imported history. Hosted lead-creation messages are explicitly marked because
their event timestamp can precede the CRM insertion timestamp. Initial summaries
are at most 500 Unicode characters. Subsequent calls receive only messages after
the previous watermark and emit additions of at most 150 characters; the merged
summary stays within 500. Older summaries are not reused as evidence on their
next regeneration. Empty or duplicate additions preserve existing text.

Qualification generation receives the compiled organization AI Playbook, application state,
and scoped messages. It must return exact inbound answer quotes, message IDs,
and the preceding outbound question. Python rejects unknown question IDs, fabricated quotes, outbound answers and unsupported question/answer pairs. No
unrelated CRM notes or conversation summaries are sent to this extractor.
System notes stay within 500 characters including their header; updates add at
most 150 characters. Legacy AI note contents are replaced on the next valid
qualification update. Manual notes are unchanged.

The current runtime also records bounded `AITrace` rows, durable `AIActionReceipt`
rows for source-bound CRM mutation idempotency, and source-backed `LeadSignal`
observations. These are internal execution evidence; they are not injected into
customer answers as hidden reasoning and they do not replace CRM/qualification state.

Direct customer questions are resolved before the next qualification question is
composed. The response may combine a grounded answer/acknowledgement with the next
unanswered Playbook question, but the system must not skip the requested answer to
force the questionnaire forward.

Current deployments include AI migrations `0015` through `0018`, including the
canonical `OrgInfo.ai_playbook` migration. Use the repository's existing CI,
migration and staging→main release workflow; web and workers must run compatible code
around schema changes.

## Organization model routing (October 2026)

Superadmin configures qualification, sales-support, and post-turn summary models
for each organization. New Lead/New Leads uses the qualification model; another
concrete stage uses the sales-support model. The turn's model selection also
applies to its grounding check, malformed-verdict retry, grounding reply repair
and recheck, and optional evidence-coverage assessment. A blank selection keeps
platform defaults. Existing unavailable-model fallback remains in the shared
provider; the configured organization choice is not rewritten.

This routing does not add provider calls or remove validation. A reply-generation
job plus a post-delivery summary job is not an exact two-call guarantee: an
independent grounding call, bounded repair, optional evidence review, or final
language composition may add calls. Summary generation retains its separate
configured model.
