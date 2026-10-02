---
name: shvya-knowledge-manager
description: Manage Shvya AI Brain FAQs, URL sources and uploaded knowledge documents, including ingestion health, publication and safe retirement.
---

# Shvya knowledge manager

Use for FAQs, business URLs, files, ingestion failures, stale knowledge and knowledge-grounding readiness.

## Workflow

1. Read `get_knowledge_health`, `list_faqs` and the AI Brain context. Knowledge health metadata does not prove document contents answer a specific question.
2. Decide whether each fact belongs in About, FAQ, URL knowledge or a document. Keep operating instructions in the Playbook.
3. For FAQs use `upsert_faq`; for URLs use `create_knowledge_source`; for files use `upload_knowledge_document` then `publish_knowledge_document` only after processing/embedding succeeds.
4. Never claim upload equals ingestion or ingestion equals correct retrieval. Verify processing state, active version, chunk/embedding coverage and publication.
5. Preserve version history. Archive obsolete FAQs/documents instead of deleting evidence by default.
6. When an AI answer misses known knowledge, combine knowledge health with `get_production_trace`/AI diagnostics to determine whether retrieval, grounding, prompt policy or provider execution failed.
7. Re-run AI policy/acceptance tests after meaningful knowledge changes.

## Guardrails

Do not retrieve secrets, signed storage URLs or raw vectors. Do not publish uncertain prices/policies as facts. Do not infer a file's contents from its filename.

## Output

List active sources, ingestion/publication state, changes, failures by layer, tests performed and unresolved source-quality questions.

## Shared quality contract

Before material live work, read the shared [skill quality contract](../../framework/skill-quality-contract.md), [evidence and attribution model](../../framework/evidence-and-attribution.md), [known-trap method](../../framework/known-trap-method.md), [execution and recovery contract](../../framework/execution-and-recovery.md), and this skill's [domain checks](references/domain-checks.md). Load only what the task needs, but do not report a defect before applying the relevant trap check.

Treat `evals/evals.json` as behavioral acceptance rubrics, not executed test evidence. A successful tool response is never sufficient on its own: verify authoritative read-back and the requested business effect at the strongest evidence level available. Keep UNKNOWN, conflicting and unavailable evidence explicit instead of guessing.
