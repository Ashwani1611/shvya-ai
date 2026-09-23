# SHVYA architecture

SHVYA is a Django modular monolith backed by PostgreSQL/pgvector, Redis, Celery
and Django Channels. The Hosted WhatsApp linked-device gateway is the intentional
Node.js exception.

Core ownership boundaries:

- CRM is the system of record for leads, pipelines, stages, attributes and activity.
- AI engagement proposes bounded decisions; Python permission and execution
  contracts own authoritative mutations.
- Messaging, Sales, Calendar, Support and Telephony remain domain-owned.
- PostgreSQL is durable state; Redis is cache/broker/realtime transport.
- Django Templates + HTMX/JavaScript are presentation layers; backend validation
  remains authoritative.
- Organization identity is a security boundary across HTTP, WebSockets, tasks,
  provider routing, cache/idempotency keys and analytics.

See [architecture-boundaries.md](./architecture-boundaries.md) for service
ownership, CRM modularization, AI runtime constraints and the safe future
`apps.channels` rename plan.
