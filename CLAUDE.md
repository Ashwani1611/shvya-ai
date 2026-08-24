# SHVYA AI — Technical Architecture Rules

SHVYA is a multi-tenant SaaS CRM. AI and automation operate on top of the
CRM as the source of truth — never the other way around.

## Stack

- Backend: Python + Django + Django REST Framework
- Database: PostgreSQL + pgvector (pgvector needed from Phase 4 onward)
- Queue: Redis + Celery
- Authentication: JWT (SimpleJWT) for app users, org-scoped API keys
  (apps/organizations/models.py APIKey — key_prefix + hashed key_hash,
  never store the raw key) for server-to-server access
- Frontend (V1): Django Templates + HTMX + JavaScript + Tailwind
- Architecture: Multi-tenant, service layer, event-driven, queue-first

---

## Current actual model shape

**Always check the actual model file before assuming a field exists.**

### Organization

- id
- name
- timezone
- plan
- is_active
- settings
- created_at
- updated_at

### APIKey

- id
- organization
- name
- key_prefix
- key_hash
- can_upsert_leads
- last_used_at
- expires_at
- is_active
- created_at

API keys are issued through `APIKey.issue()`.

Never store the raw API key.

### User

- id
- organization
- name
- email
- phone
- role
- is_active
- is_staff
- last_login_at

Roles:

- admin
- agent

Email is globally unique, not organization-scoped.

### Pipeline

- id
- organization
- name
- description
- is_active
- created_at
- updated_at

Unique:

- `(organization, name)`

### Stage

- id
- pipeline
- name
- display_order
- color
- is_active
- config
- created_at
- updated_at

Unique:

- `(pipeline, name)`
- `(pipeline, display_order)`

### Lead

- id
- organization
- pipeline
- stage
- name
- phone
- email
- notes
- attributes
- created_at
- updated_at

Unique:

- `(organization, phone)`

Phone is normalized to:

`+<countrycode><digits>`

through `normalize_phone()` in `Lead.clean()`.

The Lead model does NOT contain:

- ai_score
- source
- owner
- status
- priority
- company_name

Do not assume these fields exist.

Always inspect:

`apps/crm/models/lead.py`

before referencing Lead fields.

### LeadContact

- id
- lead
- channel
- handle
- verified
- metadata
- created_at

---

# Rules

## 1. Tenant isolation

Never bypass tenant isolation.

Every organization-owned query must be scoped to the authenticated
user's organization / `organization_id`.

Never expose data belonging to another organization.

## 2. Business logic

Business logic belongs in `services/`.

Do not move substantial business logic into:

- views
- viewsets
- serializers
- templates

Views should primarily handle HTTP concerns and delegate business logic
where appropriate.

## 3. Async / background operations

All background and asynchronous operations go through Celery.

Never call an LLM, WhatsApp API, email provider, or other expensive
external automation synchronously from a Django view.

## 4. Queue-first architecture

Every meaningful AI or automation trigger enters the event/job system
rather than directly executing complex automation.

Queue-first is non-negotiable.

## 5. Idempotency

All external messages must support idempotency.

Use:

- `external_id` for messages
- `Idempotency-Key` for API writes

Never create duplicate external actions because of retries.

## 6. Approved frontend/backend stack

Do not introduce:

- React
- Next.js
- FastAPI
- Node

unless explicitly approved.

The V1 frontend remains:

- Django Templates
- HTMX
- JavaScript
- Tailwind

## 7. Work one Django app at a time

Do not modify unrelated Django applications in a single change.

Keep changes scoped to the feature being implemented.

## 8. Check actual models first

Before adding a field or referencing one in:

- `admin.py`
- `views.py`
- `services/`
- serializers
- forms
- templates

open and inspect the actual model file first.

This project has previously had admin/service code referencing fields
that did not exist on the model.

Never guess model fields.

## 9. Secrets

Never expose secrets.

Read credentials from `.env` through `python-decouple`.

Never commit:

- API keys
- passwords
- JWT secrets
- provider credentials
- `.env`

## 10. Model changes and migrations

Write a migration for every model change.

Immediately after editing a model:

```text
python manage.py makemigrations