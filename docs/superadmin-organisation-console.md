# Superadmin organisation console

The organisation detail page retains existing SHVYA navigation, forms, routes,
permissions, payments, user and pipeline actions, integrations, notes, tags,
model routing, Vault and external AI controls. The console adds the compact
filter/card/table/chart arrangement inspected in the authenticated Kraya
organisation dashboard on 17 October 2026. No Kraya client records or assets
are imported.

## Added views

- Date range and hourly/daily/monthly granularity; organisation autocomplete searches name, email and phone; Enter opens
  the existing searchable organisation directory.
- Business description, AI Playbook and bot-language presence checks.
- Account/onboarding state, last lead timestamp and overdue follow-up count.
- All-time and selected-period lead totals; source, current stage and current
  pipeline breakdowns of leads created in the selected period.
- WhatsApp inbound/outbound, delivered/read, failed and queued counters. These
  count messages by creation time and current status, not delivery-event time.
- Lead creation and outbound WhatsApp creation charts; successful email and
  WhatsApp Cadence execution charts use finished_at. Accessible data tables are
  available alongside every chart. Zero buckets are filled.
- The next 20 active, enabled, scheduled Cadence assignments and their next step.
  Overdue is a scheduling observation, not proof of failure.

Every query uses the URL-authorized organisation; the existing superuser guard
remains unchanged. Operational test lead fixtures are excluded. Windows use the
organisation timezone, inclusive calendar end dates, and an exclusive next-day
boundary. Ranges over 367 days fall back; hourly ranges are limited to seven days.
Read-only dashboard requests do not create configuration or wallets. No schema migration is needed. Explicit knowledge indexing/repair actions queue existing background services and can consume AI coins.

The client-health card contains explicit facts, not an invented Kraya risk score.
Kraya's separate global marketing/sales/AI-calling reports, proprietary health
scoring, browser-extension controls, and LangSmith workspace are not replicated
by this organisation-page change. SHVYA's existing equivalents remain available.

## Verification

Run `pytest apps/superadmin/tests/test_organization_analytics.py`. Tests cover
local-day boundaries, tenant isolation, date validation, anonymous rejection,
and rendering of both existing controls and added sections.


## Organisation configuration

The expanded account card exposes creation time, next payment/renewal, sale date,
onboarding completion, seat count, active/disabled state and timestamp, POC,
package, timezone and user contacts. The existing organisation editor and account
activation controls remain. Next payment follows SHVYA's existing renewal-date
property; no separate installment schedule is invented.

The credit card reads the canonical AI wallet (available after reservations,
lifetime usage/additions, reserved amount, threshold and blocked state). Existing
coin management, audit history and alerts are linked. Legacy organisation credits
remain separately visible and editable through the existing organisation editor.

The new per-organisation AI Brain editor supports:
- Business description, bot languages and the canonical AI Playbook.
- Organisation AI replies, bump-up enablement and maximum count.
- Website knowledge URLs and file uploads through existing validation services.
- Document processing state, indexed chunks and explicit repair preview/queue.
- AI-guided sending instructions with stored-file validation.
- FAQ creation, editing and activation.
- Pipeline and stage AI switches, and existing advanced model routing.

Mutations require superuser access and CSRF, use the URL organisation, and record
an audit event without copying Playbook/business content into audit metadata.
Existing-record edits use signed revision tokens to reject concurrent edits.
Knowledge ingestion is queued after transaction commit; no provider call runs in
the save request. File-sharing validation does not send a message. Source removal
is deliberately not exposed because SHVYA's canonical service marks it legacy.

## Additional verification

Run `pytest apps/superadmin/tests/test_ai_brain_console.py
apps/superadmin/tests/test_organization_analytics.py` (17 tests). These exercise
scoped saves, stale edits, FAQ changes, pipeline/stage tenant boundaries, upload
validation/dispatch, website dispatch, wallet accounting, and existing page
controls. Website DNS is mocked to a public address; task dispatch is mocked, so
these tests do not claim live provider ingestion or delivery. Tests ran using the
local PostgreSQL-compatible PGlite runtime; normal PostgreSQL CI remains required.
Django system checks report the pre-existing SUPPORT_PUBLIC_BASE_URL warning.
The cloud browser could inspect authenticated Kraya but could not reach the local
preview; pixel-level browser verification and deployment remain outstanding.
