# SHVYA AI — Engineering Contract

This file defines the engineering rules that must be followed when changing the SHVYA AI codebase.

SHVYA AI is a multi-tenant AI sales engagement and CRM platform. The CRM is the system of record. AI, messaging, automation, analytics, and external integrations operate on top of tenant-scoped CRM data and must never bypass or replace that source of truth.

For project setup and product overview, see [`README.md`](./README.md). For feature-specific operational documentation, see [`docs/`](./docs/).

---

## 1. Source of truth

Do not code from assumptions, old prompts, screenshots, memory, or stale documentation.

Before changing a feature, inspect the current implementation that owns it:

- models and migrations
- URLs and views
- serializers and forms
- service-layer code
- Celery tasks and routing
- templates and JavaScript
- tests
- settings and environment examples
- integration-specific documentation

If documentation and code disagree, verify the intended behavior using the current implementation and tests before making a change. Update stale documentation as part of the same change when appropriate.

Never invent model fields, routes, settings, provider behavior, queue names, or database columns.

---

## 2. Current platform stack

The approved application stack is:

- **Language:** Python 3.13
- **Backend:** Django 6.1 + Django REST Framework
- **Authentication:** SimpleJWT for application APIs and organization-scoped API keys for server-to-server access
- **Database:** PostgreSQL 17 + pgvector
- **Cache / broker:** Redis 8
- **Background processing:** Celery 5.6 + Celery Beat
- **Realtime:** Django Channels + Daphne + channels-redis
- **AI:** OpenAI SDK + LangGraph
- **Frontend:** Django Templates + HTMX + JavaScript + Tailwind CSS
- **Production HTTP:** Gunicorn + Nginx
- **Containers:** Docker + Docker Compose
- **CI/CD:** GitHub Actions

There is one intentional Node.js service:

- `whatsapp_web_gateway/` uses Node.js 18+, Express, Redis, and `whatsapp-web.js` for Hosted WhatsApp linked-device sessions.

Do not introduce React, Next.js, FastAPI, a second frontend framework, or another Node.js application into the Django product unless explicitly approved. The existing Hosted WhatsApp gateway is the exception, not a precedent for moving product logic out of Django.

---

## 3. Repository boundaries

Use the existing structure instead of creating parallel architectures.

```text
apps/                    Django domain applications
services/                Business and provider integration logic
core/                    Shared cross-cutting utilities
config/                  Settings, URL routing, ASGI/WSGI and Celery config
templates/               Server-rendered UI
static/                  JavaScript, CSS and other static assets
tests/                   Cross-application tests
docs/                    Operational and feature documentation
whatsapp_web_gateway/    Internal Hosted WhatsApp Node.js gateway
```

### Responsibility rules

- **Models** own persisted state and database-level invariants.
- **Views** handle HTTP concerns and delegate meaningful business work.
- **Serializers/forms** validate and transform request data. They are not the primary business layer.
- **Services** own substantial domain logic, provider calls, orchestration, and reusable workflows.
- **Celery tasks** should be thin execution boundaries around service-layer work where possible.
- **Templates/JavaScript** handle presentation and client interaction, not authoritative CRM or provider business rules.
- **`config/`** owns environment/runtime wiring, not feature business logic.

Do not duplicate an existing service in a new location simply because it is easier to patch locally.

---

## 4. Multi-tenant isolation is mandatory

Tenant isolation is a hard security boundary.

Every query for organization-owned data must be scoped to the authenticated user's organization or to an explicitly authorized organization context.

Rules:

1. Never fetch tenant-owned rows by primary key alone when the request is organization scoped.
2. Never trust a client-supplied `organization_id` when the authenticated context already determines the tenant.
3. Validate cross-model relationships before saving. A related object from another organization must never be attachable to the current tenant.
4. Tenant scope must also be present in caches, distributed locks, deduplication keys, background jobs, and provider-account lookups.
5. Cross-organization access is permitted only for explicit Superadmin flows and must remain in the appropriate administrative boundary.
6. Do not weaken tenant filtering to solve a UI or integration bug.

When adding a query, ask: **What prevents this row from belonging to another organization?** If the answer is nothing, the query is incomplete.

---

## 5. Authentication, users, and API keys

The current user roles are defined in `apps/accounts/models.py`:

- `superadmin`
- `admin`
- `agent`

Current invariants:

- Superadmins do not belong to a client organization.
- Non-superadmin users must belong to an organization.
- User email is globally unique.

Organization API keys are defined in `apps/organizations/models.py`.

API-key rules:

- issue keys through the existing `APIKey.issue()` path
- store only the prefix and secure hash
- return the raw key only at issuance
- never persist or log the raw key
- enforce expiration, activation state, permissions, and organization scope
- do not convert API-key authentication into a global credential

JWT secrets, API keys, access tokens, refresh tokens, webhook secrets, SMTP credentials, database passwords, and provider credentials must never be committed or logged.

---

## 6. CRM invariants

Always inspect the actual model before modifying CRM behavior.

The Lead model currently lives at:

`apps/crm/models/lead.py`

Important current invariants include:

- leads belong to an organization
- leads belong to a pipeline and stage
- the pipeline must belong to the same organization
- the stage must belong to the selected pipeline
- phone numbers require a country code and are normalized to `+<digits>`
- `(organization, phone)` is unique
- lead AI enablement is stored explicitly
- lead source is stored explicitly

Do not recreate removed or historical fields such as `owner`, `status`, `priority`, or `company_name` unless the current model and product design explicitly require them.

When business data is represented through `attributes`, pipeline configuration, related models, or another existing source, use that source rather than adding a duplicate field.

Database constraints and validation should protect important invariants in addition to UI validation.

---

## 7. Business logic belongs in services

Meaningful business logic should live in `services/` or an established domain service module.

Avoid large workflows in:

- Django views
- DRF viewsets
- serializers
- forms
- templates
- JavaScript

Views should primarily:

1. authenticate and authorize
2. parse/validate request data
3. call a service or enqueue work
4. translate the result into an HTTP response

Do not create a second implementation of provider or CRM logic for a new UI screen. Reuse the same service contract.

---

## 8. Queue-first and asynchronous architecture

Network-bound, expensive, delayed, retryable, or customer-message automation work should execute through Celery rather than blocking a Django request.

Do not synchronously call an LLM, perform long provider operations, run large ingestion jobs, or execute scheduled automation from a request/response path unless the existing architecture specifically requires a short synchronous exchange such as an OAuth callback.

The current Celery topology includes:

- default/general worker
- `ai_realtime` queue for latency-sensitive WhatsApp API AI engagement and delivery
- `hosted_ai` queue for Hosted WhatsApp AI engagement
- Celery Beat for recurring dispatch/recovery work

Task routing is defined in `config/celery.py`.

Do not move customer-facing realtime work back onto the general worker if doing so can create head-of-line blocking from ingestion, summaries, follow-ups, or other background jobs.

### Async rules

- Keep tasks retry safe.
- Keep retries bounded.
- Do not use infinite retry loops.
- Do not use long `sleep()` calls inside workers as a scheduling mechanism.
- Use durable state for work that must survive deploys or broker interruptions.
- Prefer `transaction.on_commit()` when enqueueing work that depends on newly committed database state.
- Persist enough state to recover or reconcile important external actions.

---

## 9. Idempotency and deduplication

External actions must tolerate retries and duplicate webhooks.

Use stable provider IDs and existing deduplication mechanisms wherever available.

Examples include:

- provider message IDs / `external_id`
- webhook event IDs
- idempotency keys for supported API writes
- organization/account-scoped Redis locks
- database uniqueness constraints

Never rely on "the provider probably sends this once".

A retry must not create duplicate leads, duplicate messages, duplicate CRM actions, duplicate payments, duplicate workflow execution, or duplicate provider subscriptions.

---

## 10. AI engagement and knowledge rules

AI is an execution layer on top of CRM and connected knowledge. It is not an independent source of business truth.

Rules:

1. Preserve the existing engagement/qualification response contracts.
2. Use the established AI engagement service and LangGraph workflow instead of adding parallel prompt pipelines.
3. Keep provider model names configurable through environment/settings values.
4. Never hard-code a production OpenAI credential or model secret.
5. Keep customer-facing LLM calls out of synchronous views.
6. CRM mutations proposed by AI must pass the same authorization, validation, tenant, and data-integrity rules as human actions.
7. Knowledge retrieval may ground an answer, but retrieved text must not silently override authoritative CRM state.
8. Keep prompt/context growth bounded. Use existing summary and retrieval mechanisms rather than sending unbounded conversation history.
9. Preserve AI credit accounting and usage measurement when changing model execution paths.
10. Do not fabricate an answer when grounding rules require a fallback, clarification, or no-answer path.

Knowledge Base semantic retrieval uses PostgreSQL + pgvector. Do not replace vector storage with an unrelated service without an explicit architecture decision.

---

## 11. WhatsApp architecture

SHVYA has multiple WhatsApp connection/runtime modes. Keep them separate.

### Meta WhatsApp Cloud API

The Cloud API path uses Meta credentials, webhooks, account records, templates, campaigns, inbox functionality, and AI engagement.

Do not mix Hosted linked-device state into Cloud API account logic.

### WhatsApp Business App Coexistence

Coexistence is a Meta onboarding/runtime flow for a WhatsApp Business App number that participates through supported Cloud API capabilities.

Rules:

- keep Coexistence onboarding separate from Hosted QR login
- use the dedicated Coexistence asset-resolution and completion flow
- preserve explicit phone selection when Meta authorizes multiple numbers
- do not guess a phone number or WABA when Meta returns multiple eligible assets
- preserve history/state-sync handling and normal message webhook processing
- reuse the supported WhatsApp AI engagement path instead of implementing a second AI engine

### Hosted WhatsApp

Hosted WhatsApp uses the internal `whatsapp_web_gateway/` service and linked-device sessions.

Rules:

- keep gateway traffic internal to the Docker network
- authenticate Django → gateway and gateway → Django callbacks with the configured internal tokens
- do not expose the gateway directly to the public internet
- keep session data isolated
- do not log session credentials, QR secrets, or provider tokens
- keep Hosted inbox/media/automation behavior separate from Meta Cloud API state
- respect existing account-health and throttling controls before sending automated messages

Do not "fix" a Hosted problem by routing it through Cloud API or vice versa.

---

## 12. Meta and Instagram integrations

Meta integrations are security-sensitive external boundaries.

### Webhooks

- verify challenge requests using the configured verify token
- verify signed webhook payloads using the correct app secret where supported
- reject invalid signatures
- keep webhook handlers fast
- acknowledge valid webhook delivery promptly and enqueue expensive processing
- deduplicate provider events
- do not log full access tokens or secrets

### Instagram

SHVYA uses Instagram API with Instagram Login for professional Business/Creator accounts.

The Instagram App ID and Instagram App Secret are distinct configuration values from the generic Meta/WhatsApp application credentials.

Do not substitute `META_APP_ID` for `META_INSTAGRAM_APP_ID` or otherwise merge these credential domains.

Keep OAuth state organization scoped and validated before connecting an account.

See:

- `docs/instagram-setup.md`
- `docs/instagram-oauth-checklist.md`
- `docs/instagram-oauth-meta-source.md`

---

## 13. WebSockets and realtime delivery

Production uses separate WSGI and ASGI processes.

- Gunicorn serves normal Django HTTP through `config.wsgi:application`.
- Daphne serves WebSockets/ASGI through `config.asgi:application`.
- Redis backs channel-layer communication.

Do not expect Gunicorn's WSGI process to serve WebSockets.

When changing realtime behavior, verify both the HTTP path and the ASGI route. Do not collapse the two services without an explicit architecture change.

---

## 14. Database and migration rules

Every model/schema change requires a migration.

After editing models:

```bash
python manage.py makemigrations <app_name>
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py migrate
```

Rules:

- Never edit production database schema manually as a substitute for migrations.
- Never silently use `--fake` to bypass a broken migration.
- Review generated migrations before committing them.
- Prefer database constraints for critical uniqueness/integrity rules.
- Preserve PostgreSQL-specific features already used by the project, including pgvector, GIN/trigram indexes, and JSONB behavior.
- Add indexes based on real query patterns, not speculation.
- Separate large backfills from risky schema operations when deployment safety requires it.
- Avoid destructive migrations without a deliberate staged rollout and rollback plan.

If a migration depends on PostgreSQL extensions, make that dependency explicit and test it in CI-compatible PostgreSQL.

---

## 15. API rules

Public/application APIs are versioned under `/api/v1/` where applicable.

API changes must preserve:

- authentication
- authorization
- tenant scope
- validation
- stable response contracts
- predictable error behavior
- retry/idempotency safety for external writes

Do not expose internal model fields simply because they exist.

Do not return another organization's data in lookup, autocomplete, export, analytics, or error responses.

When changing a documented API contract, update tests and relevant documentation in the same change.

---

## 16. Frontend rules

The application frontend remains server rendered:

- Django Templates
- HTMX
- JavaScript
- Tailwind CSS

Rules:

- keep authoritative business rules on the backend
- use Django URL reversing instead of hard-coding internal application URLs when practical
- preserve CSRF protection on state-changing browser requests
- avoid duplicating the same workflow logic in JavaScript and Python
- keep HTMX partial responses scoped to the component being updated
- maintain keyboard/accessibility behavior when redesigning controls
- do not introduce a separate SPA architecture for an isolated screen

Marketing pages may use richer static JavaScript/CSS, but they must not create a second product application stack.

---

## 17. Performance rules

Performance fixes must be based on actual query/workload behavior.

Common requirements:

- eliminate N+1 queries with `select_related`, `prefetch_related`, annotations, or batch queries
- paginate large datasets
- avoid one query per lead/card/message in dashboard loops
- avoid loading full rows when only a small field set is required
- keep expensive aggregation out of templates
- use indexes that match real filters/orderings
- ensure tenant-aware cache keys
- keep external/provider calls outside database transactions when practical
- do not hold Redis/database locks longer than necessary

A change is not complete if it fixes correctness but introduces an obvious per-row database or provider call on a hot path.

---

## 18. Security rules

Never commit or expose secrets.

Secrets must come from environment/configuration files that are excluded from version control.

Never commit:

- `.env`
- production/staging passwords
- API keys
- JWT secrets
- Meta secrets/tokens
- OpenAI keys
- SMTP credentials
- Hosted WhatsApp gateway tokens
- session material

Additional rules:

- keep production `DEBUG=False`
- validate redirect/OAuth state
- validate webhook signatures
- preserve CSRF protection for browser flows
- authorize media/file access before returning it
- sanitize/validate uploads and external URLs where applicable
- do not include credentials or sensitive payloads in exception messages or logs
- use constant-time verification helpers for secrets/signatures where provided by the framework/library
- never weaken an authentication check to solve an integration problem

---

## 19. Error handling and observability

Errors should be actionable without leaking sensitive information.

Rules:

- do not swallow exceptions silently
- catch provider-specific exceptions where behavior differs
- log enough structured context to identify organization/account/job/message without logging secrets
- distinguish transient provider/network failures from permanent validation/configuration failures
- do not retry permanent failures indefinitely
- surface user-safe error messages while preserving diagnostic detail in server logs
- keep health endpoints lightweight

Health endpoints currently include:

- `/health/live/`
- `/health/ready/`

---

## 20. Testing requirements

A bug fix should include a regression test whenever the behavior can reasonably be automated.

A feature should test its important success path and meaningful failure/security boundaries.

The CI contract currently runs:

```bash
ruff check .
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest --cov
```

CI also validates both Docker Compose files and builds both application images.

Relevant files:

- `.github/workflows/ci.yml`
- `pyproject.toml`
- `requirements-dev.txt`

Do not disable tests, reduce assertions, mark tests xfail, lower coverage thresholds, or exclude code from coverage merely to make CI green unless there is a documented engineering reason.

When changing an external provider integration, mock the network boundary in unit tests but preserve realistic provider payload/response contracts.

---

## 21. Docker and runtime rules

Production and staging are separate environments.

Production stack:

- `docker-compose.yml`

Staging stack:

- `docker-compose.staging.yml`

Current service responsibilities include:

- PostgreSQL + pgvector
- Redis
- Gunicorn web service
- Daphne ASGI service
- general Celery worker
- realtime AI worker
- Hosted AI worker
- Celery Beat
- Hosted WhatsApp gateway
- Nginx
- Certbot in production

Do not collapse staging and production databases, media, sessions, secrets, or Compose project names.

Do not make manual production-container edits that are not represented in Git.

---

## 22. Deployment and branch workflow

The normal promotion path is:

```text
feature/fix/docs branch
        ↓
      staging
        ↓
   CI + staging deploy
        ↓
    verification
        ↓
       main
        ↓
   CI + production deploy
```

Rules:

1. Start normal work from the current `staging` branch.
2. Use a focused branch such as `feature/...`, `fix/...`, `chore/...`, or `docs/...`.
3. Open the first PR into `staging`.
4. Do not promote a failing CI run.
5. Verify staging when the change affects runtime behavior.
6. Promote the tested change to `main` through a PR.
7. Before a staging → main promotion, verify the diff so unrelated staging work is not accidentally shipped.
8. Production deployment is driven by the successful `main` CI workflow.
9. Staging deployment is driven by successful `staging` CI or its explicit workflow dispatch path.

Do not bypass CI for convenience.

Deployment workflows intentionally deploy the validated Git commit. Do not replace that with "whatever happens to be on the server".

---

## 23. Change discipline

Keep changes focused.

- Do not refactor unrelated apps while fixing a small bug.
- Do not rename broad public interfaces without need.
- Do not add compatibility shims indefinitely when the old implementation is no longer required.
- Remove dead code after the replacement path is proven and callers have migrated.
- Do not preserve broken behavior merely because it is old.
- Avoid broad formatting churn in functional PRs.
- Update tests when behavior changes.
- Update docs when architecture, setup, environment variables, or operator steps change.

Prefer one coherent implementation over multiple fallback implementations that disagree about business rules.

---

## 24. Prohibited shortcuts

Do not:

- bypass organization scoping
- guess model fields or database columns
- hard-code secrets
- call long-running provider/LLM work synchronously from normal views
- merge Hosted WhatsApp and Meta Cloud API state models
- skip webhook signature validation
- disable CSRF or authorization to fix a UI issue
- silently ignore provider errors
- retry permanent failures forever
- write directly to production data to compensate for missing migrations
- introduce a new framework for a single screen
- duplicate service logic inside views or JavaScript
- merge unrelated staging changes into production without checking the diff
- disable CI checks to force a merge

---

## 25. Required pre-change checklist

Before editing code:

1. Identify the owning app/service.
2. Inspect the current model(s) and migration history.
3. Inspect the current URL/view/service/task path.
4. Inspect existing tests for the behavior.
5. Check tenant boundaries.
6. Check whether the work is synchronous, asynchronous, realtime, or provider-driven.
7. Check environment/configuration dependencies.
8. Check staging/production implications.

If any of these are unclear, inspect the code rather than guessing.

---

## 26. Required pre-PR checklist

Before opening a PR, run the relevant subset and preferably the full CI-equivalent checks:

```bash
ruff check .
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest --cov
```

For container/runtime changes also validate:

```bash
docker compose --env-file .env.example -f docker-compose.yml config --quiet
docker compose --env-file .env.staging.example -f docker-compose.staging.yml config --quiet
docker build --tag shvya-ai:local .
docker build --tag shvya-whatsapp-web-gateway:local ./whatsapp_web_gateway
```

Then confirm:

- no secrets were added
- no unintended files changed
- migrations are present when required
- tests cover the behavior
- docs/env examples are updated when required
- tenant and authorization boundaries remain intact

---

## 27. Useful source references

Use these files as starting points when working in the corresponding area:

- `README.md` — project overview and setup
- `apps/accounts/models.py` — users and roles
- `apps/organizations/models.py` — organizations and API keys
- `apps/crm/models/lead.py` — lead invariants
- `apps/channels/` — WhatsApp and Instagram HTTP/runtime boundaries
- `services/channels/` — provider integration services
- `apps/ai_engagement/` — AI engagement execution contracts
- `apps/hosted_automation/` — Hosted WhatsApp AI/automation
- `apps/followups/` — cadence/follow-up execution
- `apps/triggers/` — workflow engine
- `config/celery.py` — queues and recurring jobs
- `config/settings/` — environment-specific settings
- `config/urls.py` — top-level routes and webhook endpoints
- `docker-compose.yml` — production runtime topology
- `docker-compose.staging.yml` — staging runtime topology
- `.github/workflows/ci.yml` — CI contract
- `.github/workflows/deploy.yml` — production deployment
- `.github/workflows/deploy-staging.yml` — staging deployment
- `docs/deployment_environments.md` — environment separation
- `docs/grounded-engagement.md` — grounded AI engagement
- `docs/instagram-setup.md` — Instagram integration
- `docs/smart-triggers.md` — workflow/trigger behavior
- `docs/whatsapp_ai_troubleshooting.md` — WhatsApp AI diagnostics

---

## Final principle

**Inspect first, preserve tenant and security boundaries, keep business logic centralized, make external work retry safe, test the behavior, verify staging, then promote deliberately to production.**
