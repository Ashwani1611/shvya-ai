# 06. End-to-End Production Scenarios

> Snapshot: `staging` traced from `88a71f8a02c911c5c963c9f0d8235ff60684ab17`.

This document connects the individual architecture layers into real production scenarios. Use it when debugging a user-visible behavior because each sequence shows the expected handoff between transport, CRM, AI, queues and providers.

---

# Scenario 1: Manual CRM lead creation

## Goal

An authenticated CRM user creates a lead from the dashboard and places it into a selected pipeline/stage.

```mermaid
sequenceDiagram
    actor User
    participant UI as Dashboard/HTMX
    participant Auth as CRM session auth
    participant V as lead_create_save
    participant CRM as create_lead
    participant DB as PostgreSQL

    User->>UI: Fill lead form
    UI->>Auth: POST create lead
    Auth->>V: request.crm_user
    V->>V: validate name/phone/pipeline/stage
    V->>DB: resolve user-permitted pipeline
    V->>DB: resolve active stage inside pipeline
    V->>CRM: create_lead(source=system)
    CRM->>DB: full_clean + insert Lead
    CRM->>DB: insert lead_created activity
    DB-->>CRM: commit
    V-->>UI: HX-Trigger leadCreated
    UI-->>User: Insert lead card in selected stage
```

### Routing

```text
pipeline = explicit user-selected permitted pipeline
stage = explicit user-selected active stage
lead_source = system
```

### Possible background effect

If the newly created lead is in a system New Lead/New Leads stage and the source is eligible, the lead service may queue the configured welcome behavior after commit.

---

# Scenario 2: New customer sends first WhatsApp Cloud API message

## Goal

An unknown phone messages a connected Meta WhatsApp number. SHVYA creates the CRM lead, stores the message and queues AI.

```mermaid
sequenceDiagram
    actor Lead as Customer
    participant Meta
    participant Sec as Secure webhook
    participant WH as WhatsApp handler
    participant WS as WhatsAppService
    participant DB as PostgreSQL
    participant Q as ai_realtime
    participant AI as AI worker

    Lead->>Meta: Send WhatsApp message
    Meta->>Sec: Signed webhook POST
    Sec->>Sec: Validate X-Hub-Signature-256
    Sec->>WH: Verified payload
    WH->>WS: Process inbound
    WS->>DB: Resolve account by phone_number_id
    WS->>DB: Check external_id/wamid duplicate
    WS->>DB: Resolve pipeline from display business number
    WS->>DB: Select first active stage
    WS->>DB: Upsert lead source=whatsapp_api
    WS->>DB: Insert inbound WhatsAppMessage
    DB-->>WS: Commit
    WS->>Q: on_commit AI engagement lead_id
    WH-->>Meta: HTTP success
    Q->>AI: generate_ai_engagement_response
```

### Pipeline resolution

Priority:

```text
1. active pipeline matching receiving business phone
2. active pipeline named Leads
3. first active organization pipeline ordered by name
```

Stage is the first active stage ordered by `display_order`.

---

# Scenario 3: WhatsApp AI replies to that first message

Continuation of Scenario 2.

```mermaid
sequenceDiagram
    participant Q as ai_realtime
    participant AI as AI worker
    participant DB as PostgreSQL
    participant E as EngagementService
    participant O as OpenAI
    participant CRM as CRMActionExecutor
    participant S as send_whatsapp_message_task
    participant Meta
    actor Lead as Customer

    Q->>AI: lead_id
    AI->>DB: load current lead/org/pipeline/stage
    AI->>DB: permission + account + latest inbound
    AI->>E: engage()
    E->>DB: context + qualification state
    alt organization/product knowledge needed
        E->>O: query embedding
        E->>DB: pgvector search
    end
    E->>O: structured engagement generation
    O-->>E: decision JSON
    E-->>AI: validated decision
    AI->>DB: refresh permission/account/latest message
    AI->>DB: SELECT FOR UPDATE lead
    AI->>DB: duplicate + 24h eligibility checks
    AI->>DB: persist qualification/runtime marker
    AI->>CRM: execute validated CRM actions
    CRM->>DB: deterministic mutations
    AI->>DB: create queued outbound message
    DB-->>AI: commit
    AI->>S: on_commit sender task
    S->>Meta: WhatsApp API request
    Meta-->>Lead: AI response
```

### Important stale-response rule

If another inbound message arrives while OpenAI is generating, the worker detects that the original source is no longer the latest inbound and skips the older generated reply.

---

# Scenario 4: Existing WhatsApp lead has been moved to another stage

## Goal

A lead started in the WhatsApp number's initial pipeline/stage, then an agent or AI moved it. The customer messages again.

```mermaid
flowchart TD
    OLD[Existing lead in Qualified/other stage] --> IN[New inbound on original WhatsApp account]
    IN --> LOOKUP[Find same org + phone]
    LOOKUP --> KEEP[Attach to existing lead]
    KEEP --> NORESET[Do not reset lead to number's first stage]
    NORESET --> MSG[Persist message]
    MSG --> AI[Evaluate current CRM + established transport]
```

This prevents every new inbound message from destroying human/AI CRM progression.

The AI permission service can allow the established conversation account even when the lead has legitimately moved to a pipeline whose configured number differs, provided the conversation history proves the binding.

---

# Scenario 5: Hosted WhatsApp live inbound creates a lead

## Preconditions

- Hosted session is connected.
- Its phone number maps to an active pipeline.
- `auto_lead_creation=true` in session settings.
- Message is live, inbound, non-group and not history.
- No existing lead has the peer phone.

```mermaid
sequenceDiagram
    actor Lead as Customer
    participant WA as WhatsApp network
    participant G as Hosted Node gateway
    participant D as Django Hosted callback
    participant DB as PostgreSQL
    participant Sig as Hosted signals
    participant HJ as HostedAutomationJob

    Lead->>WA: Message business number
    WA->>G: Linked-device event
    G->>D: Authenticated callback payload
    D->>DB: Resolve Hosted account/pipeline
    D->>DB: Find peer lead by phone
    D->>DB: Create lead source=whatsapp if eligible
    D->>DB: Insert inbound wweb:<id> message
    DB-->>Sig: committed message state
    Sig->>DB: register inbound automation delay
    Sig->>HJ: enqueue durable AI job if permitted
    D-->>G: callback success
```

### Routing

```text
pipeline = exact pipeline mapped to Hosted business number
stage = first active stage by display_order
lead_source = whatsapp
```

Hosted account creation itself is rejected when its business number cannot be mapped to an active pipeline.

---

# Scenario 6: Hosted AI debounce, health pause and delivery

```mermaid
sequenceDiagram
    participant Sig as Hosted signal
    participant DB as PostgreSQL
    participant HQ as hosted_ai queue
    participant Job as Hosted job worker
    participant E as EngagementService
    participant O as OpenAI
    participant HT as Hosted transport

    Sig->>DB: create/update HostedAutomationJob
    DB-->>Sig: commit
    Sig->>HQ: schedule wakeup at available_at
    HQ->>Job: process job
    Job->>DB: verify exact source is latest on exact account
    Job->>DB: check permission + Account Health
    alt account paused
        Job->>DB: requeue job at paused_until
    else healthy
        Job->>E: engage with account-scoped context
        E->>O: structured AI generation
        O-->>E: decision
        Job->>DB: final transaction + queued outbound row
        Job->>DB: persist outbound message_id in job.result
        Job->>HT: send exact queued message
        HT-->>Job: delivery result
        Job->>DB: complete/fail job
    end
```

If health becomes paused after generation, the generated message ID remains durable. The job later resumes the same queued message instead of generating new text.

---

# Scenario 7: Hosted history synchronization

## Goal

Import older linked-device messages without treating them as new live sales leads.

```mermaid
flowchart TD
    HIST[Hosted gateway history batch] --> NORMALIZE[Normalize each message]
    NORMALIZE --> ID[external_id = wweb:messageId]
    ID --> EXIST{Message already stored?}
    EXIST -- Yes --> SKIP[No duplicate insert]
    EXIST -- No --> ATTACH{Existing lead by peer phone?}
    ATTACH -- Yes --> LINK[Attach historical message to lead]
    ATTACH -- No --> STORE[Store history message without live auto-lead creation]
    LINK --> FLAG[raw payload/history semantics]
    STORE --> FLAG
    FLAG --> NOAI[Do not enqueue live Hosted AI]
```

Historical sync is intentionally different from a live inbound. The `not historical` guard prevents old chats from creating a large number of CRM leads or triggering automated replies.

---

# Scenario 8: Coexistence onboarding and history

## Goal

Connect a WhatsApp Business App number to SHVYA through Meta Coexistence and make its conversation state available.

```mermaid
sequenceDiagram
    actor User
    participant Browser
    participant Meta as Meta Embedded Signup
    participant D as Django
    participant Graph as Meta Graph API
    participant DB as PostgreSQL

    User->>Browser: Start Coexistence setup
    Browser->>Meta: Embedded Signup
    Meta-->>Browser: authorization result/code
    Browser->>D: Complete signup callback
    D->>Graph: Exchange code/token
    D->>Graph: Discover WABA + phone assets
    D->>Graph: Fetch phone metadata
    D->>DB: Store connected API-family account
    D->>Graph: Subscribe app to WABA webhooks
    D->>Graph: Request sync/history where supported
    Graph-->>D: sync events/messages
    D->>DB: ensure/attach leads and persist messages
```

Coexistence transport ultimately belongs to the Meta Cloud API family. It does not use the Hosted Node linked-device sender.

During Coexistence history import, SHVYA can ensure/create leads for synchronized conversations using API-family pipeline routing. This differs from Hosted history synchronization.

---

# Scenario 9: Google Sheets creates or updates a lead

```mermaid
sequenceDiagram
    actor User as Sheet user
    participant Sheet as Google Sheet
    participant Script as SHVYA Apps Script
    participant Hook as SHVYA Sheets webhook
    participant GS as Google Sheets service
    participant CRM as upsert_lead
    participant DB as PostgreSQL

    User->>Sheet: Edit/add row
    Sheet->>Script: onEdit/onFormSubmit
    Script->>Hook: mapped row batch + secret
    Hook->>GS: process_google_sheet_rows
    GS->>GS: map columns + normalize phone
    GS->>CRM: upsert source=google_sheets
    CRM->>DB: create or update lead
    GS->>DB: update integration counters
    Hook-->>Script: batch result
```

A 5-minute Apps Script reconciliation trigger scans appended rows missed by live events and resends them. Phone-based upsert makes resending safe at the CRM identity level.

---

# Scenario 10: Meta Lead Ad becomes CRM lead

```mermaid
sequenceDiagram
    actor Prospect
    participant Meta as Facebook/Instagram Lead Ad
    participant Hook as SHVYA Meta lead webhook
    participant Graph as Meta Graph API
    participant DB as PostgreSQL
    participant CRM as upsert_lead

    Prospect->>Meta: Submit lead form
    Meta->>Hook: leadgen event
    Hook->>DB: resolve configured page
    Hook->>Graph: fetch leadgen field_data
    Graph-->>Hook: fields + form/ad ids
    Hook->>DB: resolve active form mapping
    Hook->>Hook: map fields + normalize/recover phone
    Hook->>CRM: upsert source=meta_ads into mapped pipeline/stage
    CRM->>DB: lead + activity
    Hook-->>Meta: EVENT_RECEIVED
```

The Meta form mapping determines the exact pipeline and stage. It is not routed from a WhatsApp business number.

---

# Scenario 11: External API creates/updates a lead

```mermaid
sequenceDiagram
    participant Client
    participant Auth as API-key auth
    participant API as LeadUpsertAPIView
    participant DB as PostgreSQL
    participant CRM as upsert_lead

    Client->>Auth: API request + key
    Auth->>DB: prefix/hash lookup + org-active validation
    Auth->>API: authenticated APIKey
    API->>DB: resolve named pipeline/stage in org
    API->>CRM: upsert source=external_api
    CRM->>DB: lock existing or create new
    API-->>Client: lead_id + added/updated
```

For new leads, sufficient valid routing must be supplied. Existing leads can be updated without recreating their identity.

---

# Scenario 12: CSV import

```mermaid
flowchart TD
    UPLOAD[User uploads import file] --> TEMP[Parse/store temporary import state]
    TEMP --> MAP[Map source columns]
    MAP --> ROUTE[Select target pipeline + stage]
    ROUTE --> MODE[Choose import/duplicate mode]
    MODE --> ROWS[Process rows]
    ROWS --> PHONE[Normalize phone]
    PHONE --> DUP{Existing phone in org?}
    DUP -->|mode says skip/update| EXIST[Apply import behavior]
    DUP -->|new| CREATE[create_lead source=csv_import]
    CREATE --> DB[(Lead + activity)]
```

CSV import is a guided CRM workflow, not a live webhook integration.

---

# Scenario 13: Customer asks a pricing/product question and RAG is used

```mermaid
sequenceDiagram
    actor Lead as Customer
    participant DB as PostgreSQL
    participant E as EngagementService
    participant ES as EmbeddingService
    participant OpenAI as OpenAI
    participant Vec as pgvector

    Lead->>DB: inbound WhatsApp message persisted
    E->>DB: load recent conversation + qualification state
    E->>E: classify query as knowledge-needed
    E->>ES: embed bounded recent query
    ES->>DB: reserve embedding credits
    ES->>OpenAI: text-embedding request
    OpenAI-->>ES: 1536-d vector
    ES->>DB: settle credits
    E->>Vec: cosine search within active org knowledge
    Vec-->>E: relevant chunks
    E->>OpenAI: structured engagement with RAG evidence
    OpenAI-->>E: grounded decision
```

If query embedding fails, the current live context builder does not automatically swap to keyword/hybrid fallback. It proceeds without retrieved knowledge for that build.

---

# Scenario 14: Simple qualification answer avoids unnecessary RAG/model work

```mermaid
sequenceDiagram
    actor Lead
    participant DB as PostgreSQL
    participant E as EngagementService

    Lead->>DB: inbound "12"
    E->>DB: load backend qualification state
    E->>E: latest expected requirement = number of agents
    E->>E: deterministically bind direct answer
    E->>E: compute next backend requirement
    E-->>DB: return deterministic engagement decision
```

The backend can save the answer and ask the next configured qualification question without first embedding “12” or asking a general model what it means.

---

# Scenario 15: AI completes qualification and moves lead to Qualified

```mermaid
sequenceDiagram
    participant E as EngagementService
    participant T as AI finalizer
    participant Q as Qualification state
    participant X as CRMActionExecutor
    participant DB as PostgreSQL

    E-->>T: validated decision + final qualification updates + stage action
    T->>DB: lock source inbound + lead
    T->>Q: project/validate answer updates
    Q->>DB: persist qualification state
    T->>X: pipeline_transition(stage_id=Qualified)
    X->>DB: resolve active Qualified stage in same org
    X->>DB: canonical lead transition
    X->>DB: append qualification completion summary when applicable
```

The model supplies only an allowed stage identifier from runtime context. Python resolves the owning pipeline and performs the actual transition.

---

# Scenario 16: Newer inbound arrives during AI generation

This is one of the most important concurrency scenarios.

```mermaid
sequenceDiagram
    actor Lead
    participant DB as PostgreSQL
    participant AI as Worker for message A
    participant O as OpenAI

    Lead->>DB: inbound message A
    AI->>DB: source=A, begin generation
    AI->>O: request response for A
    Lead->>DB: inbound message B
    O-->>AI: response for A
    AI->>DB: re-read latest message
    DB-->>AI: latest=B
    AI->>AI: skip stale response for A
```

SHVYA prefers no reply over an out-of-order reply that ignores the customer's newer message.

---

# Scenario 17: Duplicate webhook delivery

```mermaid
flowchart TD
    P1[Provider delivers event] --> EXT[External ID/idempotency lookup]
    EXT -->|not seen| SAVE[Persist durable row]
    SAVE --> WORK[Queue downstream work after commit]
    P2[Provider redelivers same event] --> EXT2[Same external ID lookup]
    EXT2 -->|already seen| EXIST[Return/use existing row; no duplicate business event]
```

The exact key depends on subsystem, such as Meta `wamid`, Hosted `wweb:<messageId>`, webhook payload hash or integration-specific identifier.

---

# Scenario 18: Smart Trigger after CRM change

```mermaid
sequenceDiagram
    participant CRM
    participant DB as PostgreSQL
    participant Beat
    participant Eval as Trigger evaluator
    participant Run as Action executor
    participant Send as Provider sender

    CRM->>DB: Commit CRM mutation + TriggerEvent
    Beat->>DB: acquire advisory scheduler lock
    Beat->>Eval: evaluate unprocessed event
    Eval->>DB: create idempotent TriggerRun(s)
    Beat->>Run: execute due run
    alt WhatsApp action
        Run->>DB: create queued message
        Run->>Send: sender task
    else email/reminder/other action
        Run->>DB: persist execution/result
    end
```

The trigger event/run tables make the automation inspectable and retryable instead of hiding all state in Redis.

---

# Scenario 19: Auto-follow-up collides with live Hosted AI

```mermaid
sequenceDiagram
    participant Beat
    participant HAI as Hosted AI dispatcher
    participant HF as Hosted follow-up dispatcher
    participant API as Meta API follow-up dispatcher

    Beat->>HAI: dispatch one due Hosted AI
    alt Hosted AI dispatched
        HAI-->>Beat: dispatched
        Beat->>HF: skip this cycle / waiting_for_ai_priority
    else no Hosted AI due
        Beat->>HF: dispatch one Hosted follow-up
    end
    Beat->>API: dispatch API follow-up independently
```

This keeps a fresh customer reply ahead of an older scheduled Hosted follow-up.

---

# Scenario 20: Knowledge source refresh fails

```mermaid
flowchart LR
    OLD[(Old active Document vN)] --> SERVE[Still retrievable]
    REFRESH[Refresh source] --> NEW[Create/process vN+1 inactive]
    NEW --> EMBED[Embedding/indexing]
    EMBED -->|failure| FAIL[vN+1 failed/inactive]
    FAIL --> SERVE
    EMBED -->|success| SWITCH[Atomic publication]
    SWITCH --> OLDINACTIVE[vN inactive]
    SWITCH --> NEWACTIVE[vN+1 active]
```

A failed refresh should not replace working knowledge with an incomplete version.

---

# Scenario 21: WebSocket client misses an event

```mermaid
flowchart TD
    WRITE[Message/status committed] --> PUB[after-commit Channels publish]
    PUB --> DROP{Browser connected?}
    DROP -- Yes --> LIVE[Realtime UI update]
    DROP -- No --> MISSED[Event not seen live]
    MISSED --> RELOAD[Later HTTP/page reload]
    RELOAD --> DB[(Read canonical PostgreSQL state)]
```

Realtime delivery is not the source of truth, so a disconnected browser does not cause business data loss.

---

# Scenario 22: Organization is disabled

In the current staging snapshot, an inactive organization is intended to be blocked at multiple access boundaries.

```mermaid
flowchart TD
    REQ{Access type}
    REQ -->|Dashboard HTTP| SESSION[CRM session authorization]
    REQ -->|API key| APIAUTH[API key organization authorization]
    REQ -->|WebSocket| WSAUTH[CRM WebSocket session authorization]
    SESSION --> ACTIVE{Organization active?}
    APIAUTH --> ACTIVE
    WSAUTH --> ACTIVE
    ACTIVE -- No --> DENY[Deny / invalidate session as appropriate]
    ACTIVE -- Yes --> ALLOW[Continue]
```

The system should not rely on the UI hiding controls for disabled tenants.

---

# Scenario 23: Complete “customer message to dashboard update” chain

```mermaid
flowchart LR
    C[Customer] --> PROVIDER[WhatsApp provider]
    PROVIDER --> HOOK[SHVYA inbound callback]
    HOOK --> CRM[Lead resolve/create]
    CRM --> IM[(Inbound Message)]
    IM --> WS1[Realtime inbound publish]
    IM --> AITASK[AI job]
    AITASK --> CONTEXT[CRM + qualification + optional RAG]
    CONTEXT --> DECISION[Validated decision]
    DECISION --> ACTIONS[CRM actions]
    DECISION --> OM[(Outbound Message)]
    OM --> SEND[Provider delivery]
    OM --> WS2[Realtime outbound/status publish]
    WS1 --> DASH[Dashboard]
    WS2 --> DASH
```

This is the overall request/response cycle in one view: inbound provider event becomes durable CRM/message state; AI works from that state; validated effects become durable outbound/action state; provider and browser are updated from durable state.