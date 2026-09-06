# CRM bulk lead actions

Select individual cards or all matching leads in the current stage. The selection toolbar appears only while leads are selected. Stage switches and table replacements clear selection; exports and cancelled dialogs retain it. All leads matching the current stage and filters are rendered by the existing dashboard, so select-all includes the complete matching stage rather than a page-sized subset.

Update Leads supports pipeline/stage movement and assignment or clearing of Auto Followup sequences. Updates are opt-in; untouched fields remain unchanged. The existing transition and follow-up services preserve history, stage timing, trigger signals, sender validation, and scheduling. Assigned sequence names now appear on lead cards.

Exports are XLSX files with either all core/custom attributes or a selected subset. Custom attributes include definitions and keys already present on selected leads. Text values, including phone numbers and strings beginning with `=`, remain literal text. Datetime columns use UTC ISO timestamps.

Delete requires an explicit confirmation dialog. The server rechecks permissions and the original stage/pipeline selection before permanently deleting leads and cascading related records. Missing, inaccessible, or moved leads reject the whole selection. Updates and deletions lock selected leads and run in one transaction; a later failure rolls back earlier changes.

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
