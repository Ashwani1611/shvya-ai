# 07. Architecture Source-Code Map

> Snapshot: `staging` traced from `88a71f8a02c911c5c963c9f0d8235ff60684ab17`.

This file maps architecture responsibilities to implementation locations. Use it as the starting point when you know **what behavior you want to change** but not **which file owns it**.

The map intentionally distinguishes entry points, orchestration, deterministic business services, durable models and provider adapters.

---

## 1. Project/runtime configuration

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Root HTTP routing | `config/urls.py` | Connects dashboard, API, webhook, auth and AI routes. |
| ASGI HTTP/WebSocket split | `config/asgi.py` | HTTP -> Django; WebSocket -> Channels with CRM session middleware. |
| Django base settings | `config/settings/base.py` | Apps, middleware, PostgreSQL, Redis, Channels, Celery, OpenAI defaults. |
| Celery queues and Beat schedule | `config/celery.py` | `ai_realtime`, `hosted_ai`, recovery/scheduler intervals. |
| Environment overrides | `config/settings/dev.py`, `prod.py`, `testing.py` | Environment-specific configuration, including Hosted app installation. |

---

## 2. Authentication and tenant access

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Dashboard/Superadmin area session loading | `apps/accounts/middleware.py` | Dedicated area sessions, session-hash verification, CRM authorization. |
| CRM WebSocket authentication | `apps/accounts/channels_middleware.py` | Resolves CRM session for Channels connections. |
| Organization access policy | `apps/organizations/access.py` | Central active/authorized CRM organization checks in current staging. |
| API key model | `apps/organizations/models.py` | Prefix + hash storage, tenant ownership and capabilities. |
| Lead API authentication | CRM API authentication classes used by `apps/crm/views/api.py` | API-key scoped access. |

When adding a new tenant-facing entry point, reuse central organization authorization rather than checking only `user.is_active`.

---

## 3. CRM models and lead lifecycle

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Lead/Pipeline/Stage models | `apps/crm/models/` | Core CRM state and constraints. |
| Canonical lead creation/upsert | `services/crm/lead_service.py` | Use this instead of duplicating create/upsert logic. |
| Pipeline/stage transitions | `services/crm/lead_transition.py` | Canonical move logic and activity consistency. |
| Lead activity recording | `services/crm_activity_service.py` | Lead creation, stage/pipeline changes, reminders, notes, calls. |
| Custom attributes | `services/crm/attribute_service.py` | Definition-aware lead attribute writes. |
| CSV/file import state/parsing | `services/crm/lead_import_service.py` | Temporary import state and parsing helpers. |
| Dashboard CRM views | `apps/crm/views/dashboard.py` | Manual lead creation, import, lead UI actions. |
| External lead API | `apps/crm/views/api.py` | API-key lead upsert and CRM API operations. |
| Bulk CRM actions | `apps/crm/views/bulk.py` and related services | Multi-lead operations/permission paths. |

### If changing how a lead is created

Start with:

```text
services/crm/lead_service.py
```

Then inspect each caller in:

```text
apps/crm/views/dashboard.py
apps/crm/views/api.py
apps/integrations/services/google_sheets.py
apps/integrations/views/meta_leads.py
services/channels/whatsapp_service.py
services/channels/hosted_whatsapp_service.py
services/channels/whatsapp_coexistence_service.py
```

---

## 4. WhatsApp Cloud API / Meta transport

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Public webhook security | `apps/channels/webhook_security.py` | Verify token and `X-Hub-Signature-256`. |
| Webhook view/parsing | `apps/channels/views_flat.py` | Provider payload entry handling. |
| Additional API runtime wrapping/diagnostics | `services/channels/whatsapp_api_runtime.py` | Runtime handler enhancements where installed. |
| Inbound business logic | `services/channels/whatsapp_service.py` | Account/lead routing, idempotent inbound persistence, outbound queue helpers. |
| WhatsApp message/account models | `apps/channels/models.py` | Durable account/message/template/campaign state. |
| Sender Celery task | `apps/channels/tasks.py` | Delivers durable queued WhatsApp messages. |
| Realtime UI publishing | `services/channels/realtime.py` | After-commit thread/inbox/status fan-out. |
| Lead source normalization | `apps/channels/lead_source_signals.py` | Hosted compatibility source relabeling. |

### If changing new WhatsApp lead routing

Inspect in order:

```text
services/channels/whatsapp_service.py::resolve_pipeline
services/channels/whatsapp_service.py::_first_stage
services/crm/lead_service.py::upsert_lead
apps/ai_engagement/services/ai_permissions.py
```

Changing pipeline-number mapping affects both lead creation and later AI transport permission.

---

## 5. Hosted WhatsApp

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Hosted session/account business logic | `services/channels/hosted_whatsapp_service.py` | Pipeline-number requirement, session settings, message/history persistence, live auto-lead creation. |
| Hosted gateway transport calls | `services/channels/hosted_whatsapp_transport.py` | Delivery through linked-device gateway. |
| Hosted automation coordination | `services/channels/hosted_automation_service.py` | Enqueue/dispatch/reply timing and due-state decisions. |
| Durable Hosted models | `apps/hosted_automation/models.py` | Account health, AI job, Hosted follow-up content. |
| Hosted message/job signals | `apps/hosted_automation/signals.py` | Single committed inbound -> durable AI enqueue path and due wakeup. |
| Hosted AI worker | `apps/hosted_automation/tasks.py` | Source freshness, account health, resume exact message, delivery ownership. |
| Hosted AI execution | `apps/hosted_automation/execution.py` | Account-scoped context + shared engagement engine + final transaction. |
| Account health service/guard | `services/channels/hosted_health_guard.py` and related Hosted services | Message limits, pause windows and reconciliation. |
| Hosted queue/control views | `apps/hosted_automation/queue_views.py` | Operational Hosted automation controls. |

### If changing Hosted AI

Do **not** modify only the canonical Meta task. Hosted AI has its own durable wrapper. Check:

```text
apps/hosted_automation/signals.py
services/channels/hosted_automation_service.py
apps/hosted_automation/tasks.py
apps/hosted_automation/execution.py
apps/ai_engagement/services/engagement.py
```

---

## 6. WhatsApp Business App Coexistence

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Coexistence onboarding and sync | `services/channels/whatsapp_coexistence_service.py` | Embedded Signup completion, token/assets, webhook subscription, history/state synchronization. |
| Connected account model | `apps/channels/models.py` | Coexistence ultimately persists into WhatsApp account/message models used by API-family transport. |
| Public Meta webhook | `apps/channels/webhook_security.py`, `apps/channels/views_flat.py` | Live Coexistence events arrive through Meta webhook family. |
| API-family AI runtime | `apps/ai_engagement/tasks.py` | Coexistence live messages use canonical Meta/API AI behavior. |

Do not confuse Coexistence with Hosted linked-device QR sessions. They have different onboarding, history and delivery paths.

---

## 7. Instagram

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Instagram models | `apps/channels/instagram_models.py` | Account, OAuth attempt, conversation, message, webhook delivery. |
| Instagram OAuth/webhook/views | Instagram-specific modules under `apps/channels/` | Connect/account/inbox flows. |
| Token refresh task | `apps/channels/instagram_tasks.py` | Periodic refresh, routed by Beat schedule. |
| Root webhook route | `config/urls.py` | `/webhooks/instagram/`. |

Instagram uses its own account/conversation/message models rather than sharing WhatsApp rows.

---

## 8. AI engagement orchestration

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Core engagement decision pipeline | `apps/ai_engagement/services/engagement.py` | Qualification, conditional RAG, prompt/input, response normalization/validation, generation locks. |
| AI permission hierarchy | `apps/ai_engagement/services/ai_permissions.py` | Org -> pipeline -> stage -> lead -> valid transport. |
| AI context construction | `apps/ai_engagement/services/context.py` | CRM/conversation/summary/knowledge snapshot. |
| OpenAI text adapter | `apps/ai_engagement/services/ai_provider.py` | Responses API, structured output, retry classification, credit reserve/settle. |
| Canonical AI task/finalizer | `apps/ai_engagement/tasks.py` | Rechecks state, final lock, idempotency, CRM actions, outbound message. |
| CRM action validation | `apps/ai_engagement/services/crm_actions.py` | Allowed action schemas. |
| CRM action execution | `apps/ai_engagement/services/crm_executor.py` | Tenant-scoped deterministic side effects. |
| Qualification logic | `apps/ai_engagement/services/qualification.py` and `qualification_state.py` | Requirements, answer state, completion summaries. |
| Runtime state contract | `apps/ai_engagement/services/runtime_state.py` | Flow version/state revision/message observation/finalization. |
| Organization AI profile | `apps/ai_engagement/services/organization_profile.py`, `org_info.py` | Org instructions and qualification compilation. |
| Fail-soft decision logic | `apps/ai_engagement/services/engagement_failsoft.py` | Hosted deterministic fallback behavior. |
| Execution/recovery tracking | `apps/ai_engagement/services/execution_tracker.py` | Canonical API AI recovery state. |

### If changing AI output behavior

Trace all of these, not only the prompt:

```text
engagement.py
ai_provider.py
runtime_state.py
qualification_state.py
crm_actions.py
crm_executor.py
tasks.py
hosted_automation/execution.py
```

The prompt is only one part of the contract.

---

## 9. Conversation summaries and background enrichment

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Internal summary generation | `apps/ai_engagement/services/internal_summary.py` | Builds/publishes derived conversation summary. |
| Summary task | `apps/ai_engagement/tasks.py` | Staleness check and retry. |
| Summary lock | `apps/ai_engagement/services/summary_lock.py` | Per-lead Redis lock. |
| Background enrichment | `apps/ai_engagement/services/background_enrichment.py` | Queues/coordinates derived AI enrichment. |
| Qualification summary task | `apps/ai_engagement/tasks.py` | Generates changed qualification notes. |

---

## 10. RAG and knowledge system

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Knowledge models | `apps/ai_engagement/models.py` and model modules | `KnowledgeSource`, `Document`, `Chunk`, FAQ and AI state. |
| File/URL extraction and chunking | `apps/ai_engagement/services/knowledge.py` | Supported formats, cleaning, version creation. |
| Embedding provider | `apps/ai_engagement/services/embeddings.py` | OpenAI embedding + credit accounting + 1536-d validation. |
| Embedding persistence/indexing | `apps/ai_engagement/services/embedding_index.py` | Batch/single indexing and org isolation. |
| Ingestion/index/publication orchestration | `apps/ai_engagement/services/knowledge_pipeline.py` | Safe version publication. |
| Vector/keyword/hybrid retrieval | `apps/ai_engagement/services/retrieval.py` | Organization-scoped retrieval methods. |
| Engagement-time use | `apps/ai_engagement/services/context.py`, `engagement.py` | Conditional query embedding and vector retrieval. |
| Ingestion Celery tasks | `apps/ai_engagement/tasks.py` | Async ingest/index paths. |

### If changing chunk size/model dimension

Check together:

```text
knowledge.py
embeddings.py
embedding_index.py
Chunk model/migrations
retrieval.py
```

Never change the embedding dimension in provider code without a coordinated database/vector migration.

---

## 11. AI credits

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Wallet/reservation/ledger models | AI engagement models | Durable balances and usage history. |
| Credit accounting service | `apps/ai_engagement/services/credits.py` | Reserve, settle, release, estimate/extract usage. |
| Text-provider enforcement | `apps/ai_engagement/services/ai_provider.py` | Reserve before OpenAI text call. |
| Embedding enforcement | `apps/ai_engagement/services/embeddings.py` | Reserve before embedding call. |
| Superadmin controls/UI | `apps/superadmin/` | Manual credit administration/visibility. |

---

## 12. Google Sheets integration

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Integration model | `apps/integrations/models.py` and related modules | Sheet IDs, target pipeline/stage, mapping, secret, counters. |
| Apps Script generation and row processing | `apps/integrations/services/google_sheets.py` | Edits/form events/reconciliation, field mapping, phone normalization, upsert. |
| Webhook views/routes | `apps/integrations/` URL/view modules | Authenticate integration secret and dispatch row processing. |
| Lead write | `services/crm/lead_service.py` | `lead_source=google_sheets`. |

---

## 13. Meta Lead Ads integration

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Page/form models | integration Meta lead model modules | Encrypted Page token, target pipeline/stage, mappings. |
| Webhook processing | `apps/integrations/views/meta_leads.py` | Leadgen event -> Graph fetch -> field mapping -> CRM upsert. |
| Root route | `config/urls.py` | `/webhooks/meta-leads/`. |
| Lead write | `services/crm/lead_service.py` | `lead_source=meta_ads`. |

Current security behavior around webhook signature is documented in `08-security-idempotency-failure-model.md`.

---

## 14. Smart Triggers

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Trigger rule/event/run models | `apps/triggers/models.py` | Durable rule, transactional event and execution state. |
| Beat dispatcher | `apps/triggers/tasks.py` | Advisory lock, event evaluation, due runs, send handoff. |
| Rule evaluation | `services/triggers/evaluator.py` | Matches events/timers to configured rules. |
| Action execution | `services/triggers/actions.py` | Deterministic delivery/action logic. |
| Product docs | `docs/smart-triggers.md` | Existing trigger-specific documentation. |

---

## 15. Auto follow-ups

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Sequence/state/execution models | `apps/followups/models.py` | Durable config and per-lead progress. |
| Periodic dispatcher | `apps/followups/tasks.py` | Hosted AI priority + Hosted/API follow-up dispatch. |
| Hosted/API dispatch coordination | `services/channels/hosted_automation_service.py` | Provider-lane scheduling/pacing. |
| Hosted step attachment/content | `apps/hosted_automation/models.py` | Hosted-specific authored content. |

---

## 16. Co-Pilot

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Lead flags/scan state | `apps/copilot/models.py` | Cached durable flags and scan freshness. |
| Periodic refresh task | `apps/copilot/tasks.py` | Beat every 30 minutes in current config. |
| API | `apps/copilot/urls.py` and views/services | Dashboard/API consumption. |

---

## 17. Analytics

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Analytics stage semantics | `apps/analytics/models.py` | Maps organization's stages to hot/won/lost meanings. |
| Analytics views/services | `apps/analytics/` | Reporting over CRM state. |
| CRM performance indexes | CRM migrations including performance/trigram migrations | Search/report query support. |

Stage semantic mapping should not be replaced with assumptions based solely on stage name unless product behavior intentionally changes.

---

## 18. Teams and permissions

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| Team/membership models | `apps/teams/models.py` | Organization teams and roles. |
| Pipeline permission model | CRM permission model modules | User-specific pipeline capabilities. |
| User/pipeline resolution | `apps/crm/views/api.py`, dashboard helpers and permission services | Controls accessible pipelines. |

---

## 19. Realtime frontend updates

| Responsibility | Main file(s) | Notes |
| --- | --- | --- |
| ASGI router | `config/asgi.py` | WebSocket protocol routing. |
| WebSocket URL patterns | `apps/channels/routing.py` | Consumer endpoints. |
| Consumers | `apps/channels/consumers.py` and channel modules | Thread/inbox WebSocket connections. |
| CRM session WebSocket middleware | `apps/accounts/channels_middleware.py` | Tenant/user authentication. |
| Publication service | `services/channels/realtime.py` | After-commit message/status group sends. |

---

## 20. Database schema and migrations

| Responsibility | Location |
| --- | --- |
| Human-readable schema/ER reference | `database.md` |
| Django executable schema | `apps/*/models.py`, model packages |
| Schema history | `apps/*/migrations/` |
| pgvector extension | AI engagement migration enabling `VectorExtension` |
| pg_trgm extension/search indexes | CRM performance/search migrations |

`database.md` should be updated when model/migration architecture changes materially.

---

## 21. “I want to change X” quick routing

| Desired change | Start here | Then verify |
| --- | --- | --- |
| Change where new WhatsApp leads land | `services/channels/whatsapp_service.py` | Hosted routing, AI permission number mapping, tests |
| Change Hosted new-lead behavior | `services/channels/hosted_whatsapp_service.py` | history guard, Hosted signals, pipeline-number config |
| Change Coexistence sync behavior | `services/channels/whatsapp_coexistence_service.py` | Meta API message routing + idempotency |
| Change AI qualification questions | org qualification config + `organization_profile.py` | `qualification_state.py`, engagement validation, CRM completion |
| Change AI reply style | engagement prompts/instructions | response schema, grounding, qualification contract, tests |
| Add an AI CRM action | `crm_actions.py` | `ai_provider.py` schema + `crm_executor.py` + tests |
| Change AI stage movement | `crm_executor.py` | `lead_transition.py`, qualification completion logic |
| Change RAG chunking | `knowledge.py` | reindex strategy, retrieval quality, docs |
| Change embedding model | `embeddings.py` | vector dimension/model migration/credits/reindex |
| Add keyword fallback to live RAG | `context.py` | `retrieval.py`, engagement tests, failure behavior |
| Change Meta webhook verification | `webhook_security.py` | `views_flat.py`, provider verification tests |
| Change Hosted health limit | Hosted health services/models | Hosted task resume/requeue + outbound sender |
| Change follow-up scheduling | follow-up models/services/tasks | Hosted priority and sender pacing |
| Change trigger scheduling | `apps/triggers/tasks.py` | evaluator/actions/idempotency/advisory lock |
| Change realtime inbox payload | `services/channels/realtime.py` | consumers/frontend compatibility |
| Block a disabled tenant | central organization access/auth layers | HTTP, API-key, WebSocket and background paths |

---

## 22. Architecture ownership rule

Before creating a new service, check whether an existing layer already owns the invariant.

Examples:

```text
Lead identity/create/update        -> lead_service.py
Lead movement                       -> lead_transition.py
AI permission                       -> ai_permissions.py
AI language decision                -> engagement.py
AI provider call                    -> ai_provider.py
AI CRM mutation                     -> crm_executor.py
WhatsApp durable message            -> whatsapp_service.py / models
Hosted scheduling/delivery          -> hosted automation layer
Knowledge extraction                -> knowledge.py
Embedding provider                  -> embeddings.py
Vector retrieval                    -> retrieval.py
Realtime publication                -> realtime.py
```

Duplicating one of these rules in a view or new task creates drift. Prefer one canonical owner and call it from each transport.