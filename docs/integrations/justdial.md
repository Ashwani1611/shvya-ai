# JustDial → SHVYA CRM integration

Last updated: 9 October 2026

## What is verified

SHVYA could not locate a public, official JustDial developer/API specification that defines a universal lead-push schema or a self-service webhook configuration screen. On 9 October 2026, a provider-facing Postman test was supplied showing an application/json POST request with leadid, name, mobile, state and related fields, acknowledged by the receiver with plain text SUCCESS. This example establishes an additional supported account-specific contract; it is not proof that a test reached SHVYA.

The official JustDial support surfaces that are publicly discoverable are:

- Help/support: https://www.justdial.com/online-consult/helpsection
- Customer support portal: https://cs.justdial.com/
- Contact page: https://www.justdial.com/cms/contact-us
- Public support number: 88888 88888
- Public support email: support@justdial.com

The technical lead-push behavior is independently documented by multiple CRM/integration vendors:

- LeadSquared: https://help.leadsquared.com/integrate-justdial-with-leadsquared/
  - generates a custom webhook URL;
  - instructs the customer to contact the JustDial account manager to configure it;
  - explicitly says the JustDial API must be configured using the GET method;
  - provides lead capture logs for created/updated/error events.
- Kylas: https://support.kylas.io/portal/en/kb/articles/how-to-integrate-justdial-to-get-leads-in-kylas-crm
  - generates a webhook URL and asks the customer to share it with JustDial support;
  - receives leads in real time and exposes failed-lead diagnostics.
- Niswey Semka: https://www.niswey.com/semka-sync-indiamart/justdial-documentation-set-up-guide
  - says the webhook URL must be shared with the JustDial account manager and that the customer does not get a JustDial self-service webhook UI.

These sources support the provider-push webhook architecture used by SHVYA. They are not a substitute for a contractual JustDial API specification. If JustDial supplies an account-specific document or payload sample, that document becomes the authoritative contract for that customer.

## SHVYA setup flow

1. Organization Admin opens Connect Hub → JustDial.
2. Organization Admin clicks Request JustDial Setup.
3. SHVYA stores a setup request. No public webhook token is created.
4. A SHVYA Superadmin opens the organization in /superadmin/ and chooses the destination pipeline and stage.
5. Only the Superadmin can click Generate webhook URL or Rotate webhook URL.
6. SHVYA creates an organization-scoped URL such as https://shvya-ai.com/webhooks/justdial/<opaque-token>/.
7. The organization shares that exact URL with its JustDial account manager/support team and asks them to configure the CRM lead push using JSON POST or GET, following the contract supplied for that advertiser account.
8. The first live JustDial enquiry is validated in SHVYA's event log.
9. Successful events create or update CRM leads with source JustDial.

## Supported inbound methods

Supported methods: **POST application/json** (verified against the supplied Postman payload), **GET query parameters** (used by the LeadSquared connector), and **POST application/x-www-form-urlencoded**.

The method may vary by advertiser/connector configuration. Give JustDial the private URL generated for the correct SHVYA organization and tell them which method to use. Successful creates, updates and duplicate lead-ID retries return HTTP 200 and the exact plain-text body `SUCCESS`. SHVYA records the actual created/updated/ignored outcome in the event log. Invalid data returns a non-2xx response, not a false success. A paused or unconfigured connection returns HTTP 503 rather than silently acknowledging a discarded lead; actual retry behavior depends on JustDial's advertiser-account delivery policy. The webhook enforces a 256 KiB POST body limit.

## Field handling

Because no public official JustDial schema was found, SHVYA uses conservative aliases backed by multiple CRM integrations and an independently published JustDial receiver endpoint. That receiver documents the common GET fields name, mobile, leadid, leadtype, prefix, phone, email, date, category, city, area, brancharea, dncmobile, dncphone, company, pincode, time, branchpin and parentid. The first live event is still the final validation point.

| SHVYA field | Accepted source aliases |
| --- | --- |
| External lead ID | leadid, lead_id, enquiryid, enquiry_id, id |
| Name | name, customername, customer_name, fullname, full_name |
| Phone | mobile, mobileno, mobile_number, phone, contact_number |
| Email | email, emailid, email_id, email_address |
| Category | category, categoryname, product, service |
| Lead type | leadtype, lead_type, enquirytype |
| Prefix | prefix |
| Location | city, state, area, locality, brancharea, pincode, branchpin |
| Company | company, companyname |
| Date/time | date, enquirydate, time, enquirytime |
| Parent ID | parentid, parent_id |

Unknown fields remain available in the ingestion event payload for troubleshooting; SHVYA does not invent mappings for them.

## Phone normalization and deduplication

JustDial is India-focused, so a 10-digit number without a country code is normalized to +91XXXXXXXXXX. International numbers that already include a country code are preserved as +<digits>.

SHVYA normalizes Indian mobile numbers as +91XXXXXXXXXX and rejects numbers longer than the E.164 limit. A second usable phone field is tried if mobile is empty or invalid.

Successful `leadid` values are idempotency keys within a single SHVYA JustDial connection: retries are acknowledged but do not reapply changes to a CRM lead, even if the lead has since been deleted. Another enquiry with a new `leadid` and the same normalized phone updates the existing lead's JustDial attributes instead of creating a duplicate, without resetting its current pipeline/stage. Existing leads that originated outside JustDial keep their name, email and original lead-source attribution. The event log records every received push, including duplicate retries.

## CRM attributes

SHVYA creates/uses these organization-scoped attributes when data is present: justdial_lead_id, justdial_lead_type, justdial_prefix, justdial_category, justdial_city, justdial_state, justdial_area, justdial_branch_area, justdial_company, justdial_pincode, justdial_inquiry_date, justdial_inquiry_time, justdial_parent_id, justdial_branch_pin, justdial_dnc_mobile, and justdial_dnc_phone.

## Messaging safety

A JustDial marketplace enquiry creates/updates a CRM lead, but it is not treated as an inbound WhatsApp customer-service conversation. The JustDial ingestion path therefore calls the existing SHVYA lead upsert with send_welcome=False. Any later WhatsApp outreach must go through the applicable consent/template/policy logic rather than being triggered merely because JustDial pushed a lead.

## Security

- The callback token is organization-scoped.
- The organization ID is never accepted from the incoming payload.
- Unknown callback tokens return 404.
- Only Superadmin can generate or rotate callback tokens.
- Organization admins can request setup and view/copy an already-provisioned URL.
- Pipeline/stage are selected server-side by Superadmin and validated to belong to the same organization.
- Raw request headers/cookies are not stored in JustDial lead-event logs.
- Common provider credential parameters such as token, API/client key, username, password, authorization, and secret are redacted before event payloads are persisted.
- Bad lead payloads are logged and return a non-2xx response.
- Rotating a webhook immediately invalidates the previous URL.

## First live activation checklist

Ask the JustDial account manager/support team:

> Please enable CRM/API lead push for our JustDial advertiser account. Configure our SHVYA callback with POST application/json using the supplied lead payload (or GET if that is what our account supports). Send one test enquiry after activation, and share the response status and body.

Then provide the Superadmin-generated SHVYA URL.

After the test enquiry: confirm the event appears in Superadmin → Organization → JustDial; confirm the lead is in the configured pipeline/stage; confirm phone normalization and key field mapping; compare the received payload with any JustDial-provided field specification; adjust aliases only if the real account payload differs; then keep the integration enabled.
## Provider POST sample and validation

Send the provider the *actual* organization-specific URL from Superadmin → Organization → JustDial (not the `api.kraya-ai.com` endpoint in the comparison example). The callback path is `https://shvya-ai.com/webhooks/justdial/<private-uuid>/` and the token must not be disclosed publicly.

```http
POST /webhooks/justdial/<private-uuid>/ HTTP/1.1
Content-Type: application/json

{
  "leadid": "JD-SYNTHETIC-12345",
  "leadtype": "category",
  "name": "Messaging",
  "mobile": "9876543210",
  "email": "jd-test@example.com",
  "date": "2026-10-08",
  "category": "Generator Dealer",
  "area": "Ghatkopar West",
  "city": "Mumbai",
  "state": "Maharashtra",
  "brancharea": "Apollo Bunder",
  "dncmobile": 0,
  "dncphone": 0,
  "company": "Example Generator Dealers",
  "pincode": "0",
  "time": "13:10:11",
  "branchpin": "400001",
  "parentid": "PK-DEMO-123"
}
```

Expected acknowledgement: **HTTP 200** with response body `SUCCESS`.

Expected CRM result: source JustDial, phone `+919876543210`, name Messaging, email `jd-test@example.com`, destination configured by SHVYA Superadmin, and individual JustDial attributes including state Maharashtra. These are synthetic demonstration contact details; the original provider sample used different contact information. The receipt and result must be confirmed in the SHVYA organization event log and the corresponding CRM lead; a third-party `SUCCESS` response cannot prove SHVYA delivery. Do not resend real customer contact data until the correct organization and URL have been verified.
