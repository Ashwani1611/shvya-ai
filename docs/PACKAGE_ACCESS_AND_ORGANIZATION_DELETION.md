# Package access and organisation lifecycle

Packages are Free, DIY, DFY and Enterprise. Migration 0006 maps legacy Pro organisations to Enterprise.

| Package | Dashboard behavior |
| --- | --- |
| Enterprise | All modules included, including Hosted Account access. Connection prerequisites still apply. |
| DFY | Call Intelligence and SHVYA Sales hidden and blocked by default. |
| DIY | Calendar, Call Intelligence, SHVYA Sales and Instagram hidden and blocked by default. |
| Free | CRM, Insights, Connect Hub, Teams and Help & Support available; other product modules remain visible as locked links to an upgrade screen. Account settings remain accessible. |

Superadmin → organisation → Package module access grants restricted DIY/DFY modules individually. These overrides are organisation-specific, apply immediately and reset when the package changes. Free requires a package upgrade. Dashboard middleware, legacy CRM API decorators and Call Intelligence API authentication enforce access beyond sidebar visibility.

Tags can be created, renamed and deleted from the organisation's tag card. Renaming and deletion affect the shared tag across all organisations. The existing inline editor assigns/unassigns tags.

Account Controls → Delete organisation opens a confirmation page. Only a platform superadmin can submit the CSRF-protected confirmation. Deletion runs in one database transaction, removes tenant users and operational records, and preserves immutable platform audit records with stable UUID references. Individual user disable remains available. Organisation activation fields are no longer editable through Django admin; no organisation disable action is exposed in Superadmin.

Internal foreign keys use RESTRICT where necessary: individual referenced records remain protected, but a complete tenant cascade can delete related records together. Unexpected references from a different organisation block the transaction.

A durable cleanup outbox records hosted WhatsApp session IDs and FileField assets before records are deleted. Celery Beat retries gateway logout and storage deletion every minute. Shared assets referenced by surviving records are retained. Cleanup failures are visible in Django admin under Organisation deletion cleanup; no credentials or file contents are stored in the outbox. Platform audit history and backups are not erased.

Deployment requires migrations and restarted web/Celery/Beat processes. Local verification uses a temporary SQLite harness without PostgreSQL index DDL; production PostgreSQL migration verification must run in CI. No production organisation is deleted by deploying these changes.
