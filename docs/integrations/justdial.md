# JustDial → SHVYA CRM integration

Last researched: 7 October 2026

## What is verified

SHVYA could not locate a public, official JustDial developer/API specification that defines a stable lead-push schema or a self-service webhook configuration screen.

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
7. The organization shares that exact URL with its JustDial account manager/support team and asks them to configure the CRM lead push using GET.
8. The first live JustDial enquiry is validated in SHVYA's event log.
9. Successful events create or update CRM leads with source JustDial.

## Supported inbound methods

Canonical: GET query parameters.

For operational compatibility SHVYA also accepts form-encoded POST and JSON POST. This tolerance is intentionally server-side. It does not change the instruction sent to JustDial: configure the callback using GET unless JustDial provides an account-specific specification stating otherwise.

## Field handling

Because no public official JustDial schema was found, SHVYA uses conservative aliases seen across integration examples and common CRM payloads. The first live event is the final validation point.

| SHVYA field | Accepted source aliases |
| --- | --- |
| External lead ID | leadid, lead_id, enquiryid, enquiry_id, id |
| Name | name, customername, customer_name, fullname, full_name |
| Phone | mobile, mobileno, mobile_number, phone, contact_number |
| Email | email, emailid, email_id, email_address |
| Category | category, categoryname, product, service |
| Lead type | leadtype, lead_type, enquirytype |
| Location | city, area, locality, brancharea, pincode |
| Company | company, companyname |
| Date/time | date, enquirydate, time, enquirytime |
| Parent ID | parentid, parent_id |

Unknown fields remain available in the ingestion event payload for troubleshooting; SHVYA does not invent mappings for them.

## Phone normalization and deduplication

JustDial is India-focused, so a 10-digit number without a country code is normalized to +91XXXXXXXXXX. International numbers that already include a country code are preserved as +<digits>.

SHVYA's existing organization + phone uniqueness rule is used for deduplication: the first event creates a lead; later events with the same normalized phone update the same lead; every push still creates a JustDial event-log record.

## CRM attributes

SHVYA creates/uses these organization-scoped attributes when data is present: justdial_lead_id, justdial_lead_type, justdial_category, justdial_city, justdial_area, justdial_branch_area, justdial_company, justdial_pincode, justdial_inquiry_date, justdial_inquiry_time, and justdial_parent_id.

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
- Bad lead payloads are logged and return a non-2xx response.
- Rotating a webhook immediately invalidates the previous URL.

## First live activation checklist

Ask the JustDial account manager/support team:

> Please enable CRM/API lead push for our JustDial advertiser account. Configure the following callback using the GET method and send one test enquiry after activation. Please also share the account-specific field/payload specification if available.

Then provide the Superadmin-generated SHVYA URL.

After the test enquiry: confirm the event appears in Superadmin → Organization → JustDial; confirm the lead is in the configured pipeline/stage; confirm phone normalization and key field mapping; compare the received payload with any JustDial-provided field specification; adjust aliases only if the real account payload differs; then keep the integration enabled.