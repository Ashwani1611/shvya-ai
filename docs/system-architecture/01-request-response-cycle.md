# 01. Full Request / Response Cycle

> Snapshot: `staging` traced from `88a71f8a02c911c5c963c9f0d8235ff60684ab17`.

This document follows a request from the network edge through authentication, Django routing, services, PostgreSQL, Celery, external providers and realtime browser updates. It distinguishes the **HTTP response cycle** from the **business response cycle**. For a webhook, HTTP may finish in milliseconds while the final WhatsApp AI reply is produced asynchronously afterward.

---

## 1. Runtime components

```mermaid
flowchart LR
    Browser[Browser / HTMX] --> Django[Django HTTP]
    APIClient[External API client] --> Django
    Meta[Meta] --> Webhook[Webhook endpoints]
    Sheets[Google Apps Script] --> Webhook
    Gateway[Hosted Node gateway] --> Webhook

    Django --> Middleware[Middleware + auth]
    Webhook --> Middleware
    Middleware --> URL[URL resolver]
    URL --> View[View / endpoint]
    View --> Service[Service layer]
    Service --> ORM[Django ORM]
    ORM --> PG[(PostgreSQL)]

    Service --> Redis[(Redis)]
    Redis --> Worker[Celery workers]
    Worker --> PG
    Worker --> OpenAI[OpenAI]
    Worker --> MetaAPI[Meta Graph / WhatsApp API]
    Worker --> GatewayAPI[Hosted gateway]

    PG --> Channels[Django Channels publisher]
    Channels --> ChannelRedis[(Redis channel layer)]
    ChannelRedis --> WS[WebSocket consumer]
    WS --> Browser
```

### Durable vs ephemeral state

| Concern | Primary owner |
| --- | --- |
| CRM records | PostgreSQL |
| WhatsApp/Instagram message rows | PostgreSQL |
| Hosted AI jobs | PostgreSQL |
| Trigger/follow-up execution state | PostgreSQL |
| Knowledge documents/chunks/vectors | PostgreSQL + pgvector |
| AI credit reservations/ledger | PostgreSQL |
| Celery transport | Redis |
| Django cache / short locks | Redis |
| WebSocket channel layer | separate Redis DB |
| External provider source of truth | Meta/OpenAI/Hosted gateway depending on operation |

Redis loss can interrupt or delay runtime work, but durable business state is designed to live in PostgreSQL.

---

## 2. HTTP request lifecycle

A normal dashboard or API request follows this shape:

```mermaid
sequenceDiagram
    participant C as Client
    participant D as Django
    participant M as Middleware/Auth
    participant V as URL + View
    participant S as Service
    participant DB as PostgreSQL

    C->>D: HTTP request
    D->>M: middleware chain
    M->>M: session/JWT/API-key/area checks
    M->>V: authenticated request
    V->>V: transport validation
    V->>S: domain command/query
    S->>DB: ORM reads/writes
    DB-->>S: rows/result
    S-->>V: domain result
    V-->>C: HTML/HTMX/JSON/HTTP status
```

### Middleware boundary

`config/settings/base.py` installs Django's security/session/auth middleware and `apps.accounts.middleware.SHVYAAreaAuthenticationMiddleware` after Django authentication.

For SHVYA-specific areas:

- `/admin/` keeps Django's standard admin session behavior.
- `/superadmin/` uses the dedicated SHVYA Superadmin session.
- `/dashboard/` uses the dedicated CRM session.

The SHVYA middleware resets `request.user` to anonymous before resolving the dedicated area session. This prevents a default Django session from leaking identity into `/dashboard/` or `/superadmin/`.

In the current staging snapshot, dashboard authorization also rejects a user whose organization is inactive. The same organization-active rule is applied to API-key and CRM WebSocket authorization paths.

---

## 3. ASGI and WebSocket lifecycle

`config/asgi.py` uses `ProtocolTypeRouter`:

- `http` -> Django ASGI application;
- `websocket` -> `AllowedHostsOriginValidator` -> `CRMSessionAuthMiddleware` -> Channels URL router.

```mermaid
sequenceDiagram
    participant B as Browser
    participant A as ASGI
    participant Auth as CRM WebSocket auth
    participant C as Consumer
    participant R as Redis channel layer

    B->>A: WebSocket upgrade
    A->>Auth: validate origin + CRM session
    Auth->>C: authenticated scope
    C->>R: join inbox/thread group
    R-->>C: message/status event
    C-->>B: JSON WebSocket event
```

WebSockets are a projection mechanism. They do not replace database writes. `services/channels/realtime.py` publishes only after transaction commit.

---

## 4. Dashboard write request

Example: manually creating a lead.

```mermaid
sequenceDiagram
    participant UI as Dashboard/HTMX
    participant MW as CRM auth middleware
    participant V as lead_create_save
    participant LS as create_lead
    participant DB as PostgreSQL
    participant BG as on_commit hooks

    UI->>MW: POST form
    MW->>V: request.crm_user
    V->>V: validate required fields
    V->>V: resolve permitted pipeline + stage
    V->>LS: create_lead(...)
    LS->>DB: full_clean + insert Lead
    LS->>DB: LeadActivity lead_created
    LS->>BG: maybe queue New Lead welcome after commit
    DB-->>LS: commit
    V-->>UI: 200 + HX-Trigger leadCreated
```

The UI receives the HTTP response after the durable lead operation completes. A welcome/automation task is separate background work.

---

## 5. API-key lead request

`apps/crm/views/api.py::LeadUpsertAPIView` authenticates with `SHVYAAPIKeyAuthentication` and requires `api_key.can_upsert_leads`.

```mermaid
sequenceDiagram
    participant C as API client
    participant A as API-key auth
    participant V as LeadUpsertAPIView
    participant S as upsert_lead
    participant DB as PostgreSQL

    C->>A: POST /api/v1/leads/... + API key
    A->>A: hash/prefix lookup + org authorization
    A->>V: request.auth = APIKey
    V->>V: DRF serializer validation
    V->>DB: resolve optional pipeline/stage in API-key org
    V->>S: upsert lead_source=external_api
    S->>DB: lock existing phone or create
    DB-->>S: lead + created flag
    V-->>C: lead_id + added/updated message
```

For a **new** lead, canonical `upsert_lead` requires pipeline and stage. For an **existing** phone, omitted pipeline/stage can leave its current CRM routing unchanged.

---

## 6. Public WhatsApp Cloud API webhook

The public route is `/webhooks/whatsapp/`.

### Security boundary

`apps/channels/webhook_security.py` wraps the legacy handler.

- GET verification compares `META_VERIFY_TOKEN` in constant time.
- POST requires `META_APP_SECRET`.
- POST validates `X-Hub-Signature-256` over the raw request body.
- Invalid/missing verification fails before business processing.

### Business lifecycle

```mermaid
sequenceDiagram
    participant M as Meta
    participant W as Secure webhook
    participant H as Webhook handler
    participant S as WhatsAppService
    participant DB as PostgreSQL
    participant C as Celery/Redis
    participant AI as AI worker
    participant Send as WhatsApp sender

    M->>W: POST webhook
    W->>W: HMAC signature validation
    W->>H: verified request
    H->>S: handle inbound message
    S->>DB: idempotency lookup by wamid
    S->>DB: resolve/create lead if needed
    S->>DB: insert inbound WhatsAppMessage
    S->>DB: commit
    S->>C: on_commit summary task
    S->>C: on_commit AI engagement task if enabled
    H-->>M: HTTP success
    C->>AI: process lead_id
    AI->>DB: re-read permissions + latest message
    AI->>DB: persist CRM actions + outbound queued row
    AI->>C: on_commit send task
    C->>Send: send durable message
    Send->>M: Meta API request
```

The important point is that **Meta's webhook HTTP response is not the AI response**. The webhook persists the event and schedules work. The customer-facing reply is generated and delivered asynchronously.

---

## 7. WhatsApp message commit and realtime UI

For a newly persisted WhatsApp message, `queue_message_publish()` defers Channels publication until commit.

```mermaid
sequenceDiagram
    participant S as Message service
    participant DB as PostgreSQL
    participant T as transaction.on_commit
    participant CH as Channels layer
    participant R as Redis
    participant B as Browser

    S->>DB: create/update WhatsAppMessage
    S->>T: queue publish callback
    DB-->>T: commit succeeds
    T->>CH: publish_message
    CH->>R: group_send thread + inbox
    R-->>B: WebSocket event
```

Groups include a lead thread group and an organization inbox group. Status updates use a separate event type.

---

## 8. Hosted WhatsApp request cycle

Hosted WhatsApp is architecturally different from Meta Cloud API. A separate Node gateway owns the linked-device/browser session; Django remains the CRM/control plane.

```mermaid
sequenceDiagram
    participant WA as WhatsApp network
    participant G as Hosted Node gateway
    participant D as Django callback
    participant DB as PostgreSQL
    participant Sig as post_save signal
    participant J as HostedAutomationJob
    participant HQ as hosted_ai queue

    WA->>G: linked-device message
    G->>D: authenticated gateway callback
    D->>DB: persist wweb:<messageId>
    DB-->>Sig: post_save after row creation
    Sig->>Sig: reject history/non-inbound/no-lead
    Sig->>J: create/update durable AI job
    J->>HQ: on_commit due-time wakeup
    D-->>G: callback response
```

Hosted AI does not simply enqueue the canonical Meta AI task. The durable job binds:

- exact organization;
- exact Hosted account;
- exact lead;
- exact source inbound message;
- available/due time;
- result and completion status.

This prevents a second WhatsApp number for the same lead from stealing account context.

---

## 9. Coexistence request cycle

WhatsApp Business App Coexistence uses Meta Cloud API transport, not the Hosted Node gateway. Its onboarding is special, but after connection its message delivery belongs to the Meta/API family.

During onboarding:

1. browser completes Meta Coexistence Embedded Signup;
2. server exchanges authorization code for access token;
3. authorized WABA/phone assets are resolved;
4. phone metadata is fetched without re-registering the Business App number;
5. account is stored as API transport;
6. SHVYA subscribes the app to WABA webhooks;
7. state/history sync requests are initiated.

The imported Coexistence history uses a dedicated synchronization path. That path may create/attach leads while importing conversations. This differs from Hosted linked-device history, where historical sync is explicitly prevented from live auto-lead creation and AI enqueueing.

---

## 10. Google Sheets webhook cycle

The integration generates an Apps Script that posts rows to SHVYA with `X-Shvya-Sheets-Secret`.

```mermaid
sequenceDiagram
    participant GS as Google Sheet
    participant AS as Apps Script
    participant V as SHVYA webhook
    participant S as Google Sheets service
    participant DB as PostgreSQL

    GS->>AS: edit/form submit/reconcile timer
    AS->>V: JSON batch + integration secret
    V->>S: process rows
    S->>S: map columns + normalize phone
    loop max configured rows
        S->>DB: upsert org+phone into configured pipeline/stage
    end
    S->>DB: increment integration counters
    V-->>AS: processed result
```

Apps Script also runs a periodic reconciliation function so missed appended rows can be resent. Upserts are phone-idempotent at the CRM layer.

---

## 11. Meta Lead Ads webhook cycle

```mermaid
sequenceDiagram
    participant Meta as Meta Lead Ads
    participant V as /webhooks/meta-leads/
    participant Graph as Meta Graph API
    participant DB as PostgreSQL
    participant CRM as upsert_lead

    Meta->>V: leadgen event
    V->>DB: resolve configured Page
    V->>Graph: fetch lead data with stored Page token
    Graph-->>V: field_data + form/ad ids
    V->>DB: resolve active MetaLeadForm mapping
    V->>V: field mapping + phone normalization
    V->>CRM: upsert into configured pipeline/stage
    CRM->>DB: lead/activity
    V-->>Meta: EVENT_RECEIVED
```

Implementation note: this endpoint currently logs a Meta signature mismatch rather than rejecting solely on that mismatch, then attempts to validate access by fetching the lead with the stored Page token. This behavior should be treated as an explicit current-runtime characteristic, not a generic webhook recommendation.

---

## 12. AI request cycle

The AI worker's input is intentionally small: for canonical Meta/API engagement, the Celery task receives only `lead_id`.

```mermaid
sequenceDiagram
    participant Q as ai_realtime queue
    participant T as AI task
    participant DB as PostgreSQL
    participant E as EngagementService
    participant O as OpenAI
    participant X as CRMActionExecutor
    participant S as Sender task

    Q->>T: lead_id
    T->>DB: load lead/current org/pipeline/stage
    T->>DB: evaluate AI permission
    T->>DB: resolve current account/latest inbound
    T->>E: engage(org, lead)
    E->>DB: build live context
    E->>O: optional query embedding + structured text generation
    O-->>E: structured decision
    E-->>T: validated EngagementDecision
    T->>DB: re-read permissions/account/latest inbound
    T->>DB: SELECT FOR UPDATE lead
    T->>X: execute validated CRM actions
    X->>DB: deterministic mutations
    T->>DB: insert queued outbound message + AI metadata
    DB-->>T: commit
    T->>S: on_commit send task
```

The worker may finish with `skipped` even after successful generation if the conversation changed, AI was disabled, the account disconnected, a duplicate response already exists, or the 24-hour free-form messaging window expired.

---

## 13. External provider request rules

### OpenAI

`OpenAIProvider` disables SDK automatic retries (`max_retries=0`). Celery is the retry owner. Calls have bounded timeouts and output limits. AI-credit reservation happens before the provider call and is settled from actual/fallback usage afterward.

### Meta WhatsApp

The AI task does not call Meta directly. It creates a durable `WhatsAppMessage` first and dispatches `send_whatsapp_message_task` after commit.

### Hosted WhatsApp

Hosted AI also creates the durable outbound row first, but final delivery is owned by the Hosted job/transport so Account Health pacing and cooldown behavior can be enforced.

---

## 14. Transaction pattern

Preferred pattern:

```text
request/webhook
  -> validate
  -> transaction.atomic
      -> lock rows when necessary
      -> mutate canonical database state
      -> attach idempotency/audit metadata
      -> transaction.on_commit(queue async work)
  -> return HTTP response
```

Do not enqueue work that expects a database row before the transaction that creates the row has committed.

---

## 15. Error semantics

Different failures belong at different layers:

| Failure | Typical behavior |
| --- | --- |
| malformed HTTP/API input | 4xx response |
| authentication/authorization failure | 401/403 or anonymous redirect behavior |
| tenant mismatch | reject/skip, never cross tenant |
| duplicate provider delivery | return existing/no-op |
| temporary OpenAI/network failure | Celery retry |
| permanent AI provider/config failure | fail task/record reason, no blind retry |
| stale AI result because a new message arrived | skip generated result |
| provider send failure | persist failure/retry according to sender semantics |
| WebSocket publish problem | durable DB state remains source of truth |

---

## 16. Review checklist for a new endpoint

When adding a new request path, verify:

- Which authentication boundary owns it?
- How is `organization` derived?
- Are all foreign IDs scoped to that organization?
- Does business logic live in a service instead of the view?
- Does a write need `transaction.atomic()`?
- Does concurrency need `select_for_update()` or an idempotency key?
- Is async work queued only after commit?
- Is the external provider call outside a fragile long-held database lock when possible?
- What durable row proves the operation happened?
- How does a retry avoid duplicate side effects?
- Does the browser need an after-commit WebSocket/HTMX update?
- Does the flow need documentation updates in this folder?