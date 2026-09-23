# Architecture boundaries

This document records module-boundary rules that are intentionally stricter than
simple Python importability.

## Service ownership

New business logic belongs with the domain that owns it:

- CRM-specific logic: `apps/crm/services/` or an established canonical CRM
  service when the existing root service is already the shared contract.
- AI engagement logic: `apps/ai_engagement/services/`.
- Sales, Calendar, Support and Telephony logic remain inside their owning apps.
- Root `services/` is reserved for established cross-domain contracts and
  shared provider adapters.

Do not create a second implementation in root `services/` when an owning app
already has a service boundary.

## CRM dashboard

`apps/crm/views/dashboard.py` is the compatibility-facing CRM view module, but
focused workflows belong in focused modules. Lead import is owned by
`apps/crm/views/lead_import.py`; global reminder HTTP behavior is owned by
`apps/crm/views/reminders.py`; filtered table/reminder rendering is owned by
`apps/crm/views/filtering.py`.

Compatibility aliases may remain in `dashboard.py`, but business logic must not
be copied back into the monolith.

## AI runtime patch ceiling

The current AI runtime still contains compatibility installers accumulated during
rapid hardening. The approved installer set is frozen by
`test_runtime_cleanup_contract.py`.

Preferred direction for future work:

1. Put behavior into the owning service or graph node directly.
2. Delete the corresponding installer once all callers use the canonical path.
3. Preserve engagement/qualification regression tests.
4. Avoid replacing class/module methods at Django startup when a direct
   dependency can express the same behavior.

## `apps.channels` rename plan

The project has a historical Django app at `apps.channels` whose app label is
`channels`. The third-party Django Channels package has the same default label.
The runtime therefore imports the third-party package as a library and does not
add it to `INSTALLED_APPS`.

A future Python-module rename should:

1. Choose a neutral path such as `apps.messaging`.
2. Keep `AppConfig.label = "channels"` initially so existing database tables,
   migrations, content types and permissions remain authoritative.
3. Move Python imports while preserving the Django app label.
4. Run the complete migration graph, WebSocket, Celery, webhook and messaging
   regression suites on staging.
5. Treat any later app-label/table rename as a separate reviewed data migration.
