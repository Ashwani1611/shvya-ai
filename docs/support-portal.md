# Help & Support / Shvya-Ops portal

> **Implementation baseline:** verified 2026-09-20 against production `main` at `7fb74946b35f189a66f92d6ffd0677909dca4c9f`. Models, migrations, services and tests remain authoritative when later changes are deployed.

SHVYA has one support domain with two authenticated views over the same committed ticket data:

- organization users: `/dashboard/support-portal/`
- platform support staff: `/superadmin/client-portal/`

The support department presented to customers is **Shvya-Ops**. The customer portal and staff portal must not create parallel ticket state.

## Ticket ownership and context

A ticket belongs to exactly one organization and one requesting user. The server derives the organization/requester from the authenticated session rather than trusting browser-supplied tenant IDs.

A ticket may also carry a validated pipeline context. When present, that pipeline must belong to the same organization. The `context` JSON field is a bounded support snapshot, not a dump of unrelated CRM records, HTTP headers, secrets, or provider payloads.

Core ticket data includes:

- immutable generated reference
- subject
- category and nested issue
- status and priority
- optional Shvya-Ops assignee
- optional organization-owned pipeline context
- custom support-field values
- portal/email source
- merge target
- first-staff-reply and latest-public-activity timestamps
- optimistic `version`
- created/updated timestamps

Built-in status keys/behavior families remain stable even when staff rename their display labels. Custom statuses select an existing behavior family rather than inventing a second state machine.

## Messages and attention state

Public customer/staff replies and internal Shvya-Ops notes are persisted as `TicketMessage` rows. Internal notes are never customer-visible.

The blue Help & Support sidebar attention state is **derived** from the latest committed non-internal message on each visible, open ticket:

- latest Shvya-Ops public reply → organization response required
- organization public reply → response requirement cleared
- closed behavior → no response required
- viewing the ticket alone does not acknowledge it

Do not add a competing unread boolean, localStorage dismissal, or browser-only acknowledgement. See [`support-response-indicator.md`](./support-response-indicator.md).

## Access policy

Organization visibility is controlled by `OrganizationSupportPolicy`.

- Default organization access can expose company tickets to authorized users in that organization.
- `own_tickets_only` restricts ordinary users to their own tickets while organization administrators retain their established elevated access.
- Superadmin/Shvya-Ops access stays inside the platform-staff boundary.
- Ticket merges are organization-local.
- Shared-link access is capability-limited, expiring and revocable; its token is stored as a hash.

All state-changing browser requests retain CSRF protection.

## Attachments

Support uploads use private support storage and authorized download paths.

The stored attachment row keeps:

- private file reference
- original filename
- size
- SHA-256
- owning ticket message

File-count, individual-size, total-size and extension rules come from the singleton `SupportSettings`. Audio notes recorded in the browser remain local until the ticket form is actually submitted.

Do not place support uploads into a public media directory or expose raw filesystem paths.

## Email notifications and intake

Support email delivery uses a transactional outbox:

1. a committed ticket event determines recipients;
2. one `EmailDelivery` row exists per event/recipient;
3. workers claim/send/retry according to durable state;
4. delivery health is visible to Shvya-Ops without exposing mailbox credentials.

`InboundReceipt` stores a digest and safe result/reason metadata. Raw email bodies and mailbox credentials are deliberately not persisted in the receipt log.

Email intake remains disabled until its environment/mailbox configuration and support settings are ready. Browser configuration does not expose or edit mailbox secrets.

## Configuration

Shvya-Ops can manage:

- statuses
- priorities
- categories
- issues
- custom fields
- saved replies
- singleton support settings

Configuration changes are audited in `ConfigurationEvent` using changed field names rather than secret values.

Custom-field keys and types are immutable once created. Inactive fields retain historical values.

## Work items

A ticket can have staff-only `WorkItem` rows for related tasks or reminders. They must be assigned to active platform support staff. Their due/completion/notification state is separate from the customer's ticket status.

The current schema does **not** define a separate SLA-policy table. Do not document or depend on an SLA data model unless a future migration introduces one.

## Idempotency and safety

Important database guarantees include:

- ticket creation: unique requester + submission key
- authenticated reply: unique ticket + author + client key
- shared-link reply: unique ticket + client key when author is null
- email delivery: unique event + recipient
- no ticket may merge into itself
- cross-organization requester/pipeline/merge assignments are rejected

A browser retry must not duplicate a ticket or reply.

## Operational checks

Use the current test/settings stack:

```bash
ruff check .
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
pytest apps/support/tests
```

For staging acceptance, verify customer create/reply/close, Shvya-Ops public reply/internal note, category→issue validation, private attachment download authorization, response-required sidebar behavior, restricted-user visibility, email outbox health and shared-link expiry/revocation.

Promote through the repository's normal staging → verification → main flow. Support schema changes require normal Django migrations; never patch the production database manually.
