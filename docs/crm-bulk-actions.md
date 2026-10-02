# CRM bulk lead actions

> **Implementation baseline:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. This documentation commit is docs-only; runtime code, migrations, tests, and deployment configuration remain the executable source of truth.

Select individual cards or all matching leads in the current stage. The selection toolbar appears only while leads are selected. Stage switches and table replacements clear selection; exports and cancelled dialogs retain it. All leads matching the current stage and filters are rendered by the existing dashboard, so select-all includes the complete matching stage rather than a page-sized subset.

Update Leads supports pipeline/stage movement and assignment or clearing of Auto Followup sequences. Updates are opt-in; untouched fields remain unchanged. The existing transition and follow-up services preserve history, stage timing, trigger signals, sender validation, and scheduling. Assigned sequence names now appear on lead cards.

Large routing-only updates are handled differently from small interactive updates. When more than 100 leads are moved without changing an Auto Followup sequence, the web request freezes only the selected lead UUIDs—without materializing full Lead rows, notes, attributes, or related objects—and queues a `crm.bulk_move_leads` job on the ingestion worker. The worker processes 100 leads per database transaction and at most 500 leads per Celery delivery, checkpoints committed progress in Redis, then requeues the next generation of the same job. A per-job cache lock prevents overlapping redeliveries from duplicating activity/workflow events. This keeps 10,000+ lead moves bounded in both web-worker memory and Celery task duration while preserving normal stage/pipeline history, workflow trigger events, and safe redelivery.


Exports are XLSX files with either all core/custom attributes or a selected subset. Custom attributes include definitions and keys already present on selected leads. Text values, including phone numbers and strings beginning with `=`, remain literal text. Datetime columns use UTC ISO timestamps.

Delete requires an explicit confirmation dialog. The server rechecks permissions and the original stage/pipeline selection before permanently deleting leads and cascading related records. Missing, inaccessible, or moved leads reject the whole selection. Small synchronous updates and deletions lock selected leads and run in one transaction; a later failure rolls back earlier changes. Large routing-only moves use bounded 100-lead transactions and bounded 500-lead worker slices so they do not hold thousands of row locks or monopolize one Celery delivery for the duration of a 10,000+ lead move.

## Permissions

All actions require a CRM session and access through `get_user_pipelines`; organization administrators have all bulk permissions. Agents can export accessible leads. Other agent actions require the corresponding `PipelinePermission` flag:

| Action | Required flag |
| --- | --- |
| Move | `can_move_leads` on source and destination pipelines |
| Assign/clear sequence | `can_edit_leads` on source pipeline |
| Delete | `can_delete_leads` on source pipeline |

The filter-pipeline lookup also uses `get_user_pipelines`, preventing an agent from using filters to access an unowned pipeline.

## Verification

Run with the project's PostgreSQL/pgvector and Redis test services:

```sh
pytest apps/crm/tests apps/followups/test_recurring.py apps/followups/test_schedule_ui.py apps/followups/test_templates.py
python manage.py check --settings=config.settings.testing
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
node --check static/crm/bulk.js
```

Local validation: 31 tests and 11 subtests passed on PostgreSQL 18. The local Windows harness substitutes the unrelated AI embedding column with an array because pgvector is unavailable; CRM tables, transactions, constraints, signals, and queries use PostgreSQL normally. No production settings or migrations were altered for this harness.

Browser checks against a seeded local Django dashboard covered 18 assertions: empty/single/all/partial selection, opt-in updates, destination-stage reset, attribute selection, actual XLSX download, safe cancellation, stage/search selection reset, sequence assignment and clearing, visible sequence names, stage movement, confirmed deletion, mobile dialog width, and absence of JavaScript errors. Desktop and 390px mobile layouts were visually inspected. The existing dashboard query-count regression test remains at seven queries for both one and multiple leads.

No database migration or new dependency is required. Deploy the updated Django files/templates and collect the new `static/crm/bulk.js` and `static/crm/bulk.css` assets using the existing deployment process.


## Bulk Campaign handoff

The CRM bulk-selection toolbar may expose **Bulk Campaign** only when the current pipeline has an eligible connected Meta API-family WhatsApp sender (Cloud API or Business App Coexistence). Hosted linked-device accounts are not silently substituted for this action.

The campaign flow keeps the selected lead identity/pipeline scope, then creates a frozen delivery audience through the Bulk Campaign service. Campaign import/review is separate from CRM lead creation until the user confirms the reviewed audience.

Recipient/history actions follow the same source of truth:

- **WhatsApp / View chat** resolves the selected lead through the WhatsApp account linked to that lead's current pipeline and opens that exact conversation.
- **View in CRM** routes to the exact CRM lead rather than a generic pipeline view.
- **Retry** creates a bounded retry for the failed recipient using the same campaign/template snapshot; it does not silently choose a different template.
- Bulk **Retry selected** and **Export leads** controls appear only when recipient rows are selected.
- Failed-template actions in Insights use the same linked-chat / exact-lead routing contract.

See the campaign tables in [`../database.md`](../database.md) for the frozen plan, delivery, attempt, provider-event and suppression records.
