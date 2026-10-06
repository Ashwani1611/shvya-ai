# IndiaMART real-time lead intake

## Research (6 October 2026)

Requested official references:
- https://help.indiamart.com/knowledge-base/lms-crm-integration-v2/
- https://help.indiamart.com/knowledge-base/integration-of-indiamarts-lead-manager-crm-push-api-with-third-party-crms-real-time-push-of-leads/

Both official help pages failed to load during research. This implementation must not be described as fully certified against current IndiaMART documentation. The seller's sample payload and actual account activation remain acceptance checks.

Cross-checked vendor-owned setup guides:
- https://znicrm.com/guide/210/crm-integration-with-indiamart/
- https://www.msgclub.net/blog/how-to-set-up-indiamart-integration-with-msgclub/

Both describe Lead Manager → Import/Export Leads → Push API → Other → CRM platform name and webhook listener URL → OTP verification. MsgClub states IndiaMART API access is required and its IndiaMART key field is optional. Its guide links the following IndiaMART-branded test page (the schema could not be retrieved):
- https://indiamartpushtest.stoplight.io/docs/indiamart-lms-push-api-teting/branches/main/qonynsty95e7y-india-mart-leads-testing

Push receives enquiries in real time. Pull v2 is a separate API with a CRM key and scheduled requests; it is not implemented or required here. Do not present polling as real time. Subscription eligibility, retry intervals, key lifetimes and support commitments are deliberately not hardcoded from unverified secondary claims.

## Setup

1. Deploy code and run `python manage.py migrate`.
2. Organization administrator opens Connect Hub → IndiaMART → Request IndiaMART Setup. The request is stored, idempotent, and visible on the superadmin organization's page/list. It does not generate an active endpoint or send a support notification.
3. Superadmin opens Organizations → organization → IndiaMART setup.
4. Select destination pipeline/stage and generate the URL. Only the dedicated superadmin session can perform this; organization administrators and agents cannot generate or see it.
5. Copy the private HTTPS URL into the IndiaMART Push API screen as described above, using Shvya CRM as the platform name. The seller completes OTP verification.
6. Use the seller panel's test listener/sample enquiry and verify the lead and enquiry details in Shvya. A generated URL means ready to receive, not verified seller activation.

No IndiaMART-specific environment secret is needed. The deployment must already have HTTPS, canonical host/proxy settings, and existing CRM/automation dependencies configured. Treat the URL as a credential: possession permits lead submission. No verified IndiaMART signature contract was available, so the integration does not claim signature authentication or source IP verification. Avoid recording token-bearing paths in reverse-proxy/access monitoring logs.

## Receiver contract

`POST /dashboard/connect-hub/indiamart/webhook/<private UUID>/`

Accepted JSON: one enquiry object, a `RESPONSE` object, or a `RESPONSE` array (1–100 enquiries). Maximum request size 256 KiB. Requires a nonempty `UNIQUE_QUERY_ID` of at most 100 characters and a buyer phone. Numbers with an explicit +country code are normalized. Ten-digit local numbers require `SENDER_COUNTRY_ISO=IN`; explicit digit-only international numbers are accepted. Email validation follows CRM validation.

Maps SENDER_NAME/MOBILE/PHONE/EMAIL to the contact. QUERY_TIME/TYPE, SENDER_COMPANY, SUBJECT, QUERY_PRODUCT_NAME/MCAT_NAME/MESSAGE and buyer address/location go into enquiry notes; the full received enquiry is retained in an organization-scoped receipt. IndiaMART is a distinct CRM lead source and creation activity label.

Returns HTTP 200 with `{"CODE":200,"STATUS":"SUCCESS"}` after database commit, including for duplicate query IDs. Invalid payloads return 400; unknown/disabled/rotated URLs return 404; oversized requests return 413; invalid configured routing returns 503. Database failures remain retriable errors. A batch is atomic: malformed rows cause rollback rather than silent lead loss. There is no dependency on a webhook worker/broker for durable intake; new lead automation continues through the existing CRM welcome service.

Connection-row locking and `(connection, query_id)` uniqueness prevent retry duplicates. Existing organization/phone leads receive a new enquiry note without changing their stage, pipeline, name or existing notes. Deleting a lead clears the receipt’s lead pointer and erases its buyer payload; only query tombstones remain, so replays do not recreate deleted leads. Replacing a URL invalidates the previous URL immediately; disabling stops intake. The last received timestamp updates on valid deliveries, including retry acknowledgments.

## Acceptance checks

Run `apps/integrations/tests/test_indiamart.py`, Connect Hub authorization/routes, and CRM source-label tests on PostgreSQL. Verify an actual seller sample/test enquiry before calling the account connected. Test URL rotation and replay. Do not transmit customer payloads to public test tools.

## Local validation result

Django system checks passed with the existing `support.W001` configuration warning. Two Connect Hub route tests passed. Twenty-four receiver and Connect Hub authorization tests passed in an isolated SQLite/locmem harness with migrations disabled and PostgreSQL-specific indexes omitted. This verifies functional flows and permissions; it does not verify PostgreSQL migrations, locking/concurrency, or live seller delivery. Migration generation reports no model drift. Ruff unused-name checks and whitespace checks passed. PostgreSQL CI and a seller-panel test remain required.
