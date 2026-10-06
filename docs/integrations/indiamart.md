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
4. Select the destination pipeline, then select one of its active stages in the separate Stage dropdown and generate the URL. Only the dedicated superadmin session can perform this; organization administrators and agents cannot generate or see it. Server validation rejects stages from a different pipeline or organization and inactive destinations. Saving routing retains the existing URL; replacing the URL invalidates the previous one.
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

The superadmin setup page uses `Referrer-Policy: same-origin` and `Cache-Control: no-store`, including on routing-validation errors. Same-origin referrers are necessary for HTTPS CSRF verification when a browser omits the Origin header; external destinations do not receive the referrer. Generation, replacement and disabling retain Django's CSRF protection. Only the provider's token-authenticated lead receiver is CSRF-exempt.

## Initial rollout validation

Django system checks passed with the existing `support.W001` configuration warning. Two Connect Hub route tests and twenty-four receiver/authorization checks passed in an isolated SQLite/locmem harness. Main PostgreSQL CI subsequently passed 4,371 tests with 76% coverage, and both production deployments applied the IndiaMART migrations successfully. An actual seller-panel test remains required to verify seller activation and live delivery.

Setup regressions cover HTTPS generation/replacement/disabling with CSRF enforcement, invalid tokens and origins, saved routing, cross-organization routing, mismatched stages, and inactive destinations. Native Chromium tests reproduce the original `no-referrer` 403 and submit the corrected form with separate dropdowns, including resetting the stage when the pipeline changes.
