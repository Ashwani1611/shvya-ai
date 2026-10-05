# Meta Conversions API

Connect Hub → Meta Conversions API is an organization-admin workspace at
`/dashboard/connect-hub/meta-conversions-api/`. It uses the existing Django CRM,
credential keyring, Celery general worker, PostgreSQL and Beat. It does not
change WhatsApp/Instagram messaging or introduce a second frontend.

## Connect and verify

1. In Meta Events Manager, open the intended dataset/Pixel. Copy its ID and
   generate a Conversions API token under Settings. This is a dataset token,
   not the Facebook Page token used by the separate Lead Ads connection.
2. Save the ID and token in Connect Hub. Tokens are encrypted with the existing
   rotating credential keyring and are never returned to the browser.
3. Choose Meta Lead Ads only (default) or all lead sources. New Lead stages
   receive a protected `Lead` mapping; additional stages can use standard or
   custom event names. Configure no value, a static value, or an active numeric
   CRM attribute. Values require currency; `Purchase` requires both.
   Returning contacts with a real Meta Lead ID are included even when the CRM
   retains their original WhatsApp, manual or other acquisition source.
4. Select the customer fields and optional shareable custom attributes.
   Missing optional fields are omitted. The customer IP/browser must already
   be stored in lead attributes; the administrator's browser is never used.
5. Copy the code from Meta's Test Events tab. Send a test using a real lead
   currently in the mapped stage. Inspect delivery activity and the actual
   event in Events Manager. A successful HTTP response only counts as accepted
   when Meta returns `events_received: 1`.
6. For live delivery, choose Live events, enable tracking and save. Live
   payloads never contain the saved Test Events code. Field selections and the
   global toggle are persisted by Save connection; individual mapping toggles
   are saved immediately.

Meta's current API guide states that events with a Test Events code can still
be used for ads measurement/targeting. Tests therefore use actual CRM leads,
and the interface explains this behavior. Acceptance by Meta does not confirm
attribution, event match quality or campaign optimization.

## Payload and event lifecycle

- New lead saves and actual pipeline/stage changes create an immutable payload
  snapshot in `MetaConversionDelivery`. A new Meta Lead Ads acquisition ID on
  an existing lead is also captured. Ordinary name/attribute edits and repeated
  saves with an unchanged stage/acquisition ID do not emit conversions.
  Partial saves, including `stage_id`/`pipeline_id` updates, compare the
  persisted CRM state and snapshot only the customer data actually saved.
- Meta Lead Ads events use `action_source: system_generated` and
  `custom_data: {event_source: crm, lead_event_source: SHVYA AI}`. Other sources
  use the mapping's actual conversion source and omit the CRM optimization
  markers. Website events require a customer page URL and customer browser.
- `user_data.lead_id` comes exclusively from `Lead.attributes.meta_leadgen_id`.
  Shvya's UUID is separately SHA-256 hashed as `external_id`. Names, phone,
  email, gender, birth date and location are normalized and hashed. Unicode
  combining marks are preserved in names and locations; country and gender
  whitespace is trimmed before normalization, including US ZIP truncation. Meta Lead
  IDs, `fbp`, `fbc`, customer IP/browser and `ctwa_clid` remain unhashed according
  to Meta's parameter specification. Optional attribute names match the UI
  keys (`city`, `state`, `zip_code`, `country`, `ip_address`, `user_agent`,
  `date_of_birth`, `gender`, `fbp`, `fbc`, `wa_ref_ctwa_clid`). This change does
  not create ad click/browser identifiers or collect website traffic.
  Events containing only one of Meta's invalid baseline matching combinations
  (or its subsets) fail locally with an actionable message rather than being
  sent to Meta. Matching uses the identifiers actually available on each lead.
- Queue publication happens after commit. Broker interruptions retain the
  outbox row; Beat recovers due work every 30 seconds in batches of 100.
  Publication is throttled to avoid repeatedly filling the broker.
- A database lease prevents concurrent sends. Retries retain the original
  event ID, payload and event time; abandoned leases are recoverable. Network,
  rate-limit and transient provider errors get at most six automatic attempts.
  Invalid tokens, permissions and event parameters need operator correction.
  Raw Meta errors, access tokens and response bodies are never logged/stored.
- Global tracking and mapping toggles pause pending automatic deliveries.
  Explicit manual test probes can run while paused. Changing the dataset or
  disconnecting invalidates pending work for the former destination; queued
  work cannot be redirected to another dataset. Removing a mapping skips its
  pending events. Requests already in flight may complete.
- Deleting a CRM lead deletes its stored delivery snapshots and pending events,
  following the existing Lead-owned data deletion policy.
- Event timestamps are never rewritten to overcome Meta's seven-day limit.
  Failed deliveries may be retried after correction within that window.
  Manual retries validate the original payload's event time, not the outbox
  row's creation date.
  Existing CRM rows and historical spreadsheet imports are not backfilled:
  the existing bulk-import path intentionally does not emit Lead save signals.
- No events are sent until an administrator saves credentials and enables
  tracking. There are no seeded delivery counts or fabricated conversions.

## Runtime and rollout

Apply migration `integrations.0018` using the normal deployment process before
starting workers on this release. Run the existing general worker and Beat.
`integrations.deliver_meta_conversion` and `integrations.recover_meta_conversions`
use the default queue, keeping them off the realtime AI lanes. Outbound HTTPS
must reach `graph.facebook.com`; requests have bounded connect/read timeouts
and do not follow redirects.

`META_CONVERSIONS_API_VERSION` defaults to `v26.0`, the version in Meta's current
Business SDK configuration inspected during implementation. Keep it on a
supported Graph API version. The existing `CREDENTIAL_ENCRYPTION_KEY` and
fallback configuration provide encryption and rotation; no new global token
environment variable is needed because each organization supplies its own.

## Official implementation sources

Reviewed on 2026-10-05 (IST):

- [Conversions API overview](https://developers.facebook.com/documentation/ads-commerce/conversions-api)
- [Get started and token generation](https://developers.facebook.com/documentation/ads-commerce/conversions-api/get-started)
- [Direct API requests and Test Events](https://developers.facebook.com/documentation/ads-commerce/conversions-api/using-the-api)
- [CRM developer implementation guide](https://developers.facebook.com/documentation/ads-commerce/conversions-api/conversion-leads-integration/crm-integration/3-implementing-the-crm-integration)
- [Server event parameters](https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/server-event)
- [Customer information and normalization](https://developers.facebook.com/documentation/ads-commerce/conversions-api/parameters/customer-information-parameters)
- [Baseline matching requirements](https://developers.facebook.com/documentation/ads-commerce/conversions-api/best-practices#baseline-requirements-for-matching)
- [Verify setup](https://developers.facebook.com/documentation/ads-commerce/conversions-api/verifying-setup)
- [Meta Business SDK](https://github.com/facebook/facebook-python-business-sdk)

The application uses direct HTTPS requests with no additional SDK dependency.
The reference screenshots inform the controls; the presentation uses original
layouts within Shvya's existing shell.

## Verification

`apps/integrations/tests/test_meta_conversions.py` covers capture/no-op behavior,
rollback, tenant/admin/CSRF boundaries, encryption, matching, event values,
website prerequisites, duplicate delivery, leases, broker recovery, bounded
retries, redacted provider failures, mode separation and dataset changes.
Provider HTTP responses are mocked. A real Meta acceptance test requires an
organization's own dataset token and an actual eligible lead.

Run the integration tests and Connect Hub authorization tests with the normal
PostgreSQL test environment, plus Ruff, Django checks, migration drift checks,
and the JavaScript syntax check. Inspect desktop/mobile layout and the mapping,
save and Test Events interactions before release.
