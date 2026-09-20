# SHVYA Sales

SHVYA Sales is the organization-scoped sales-document workspace for quotations,
agreements and invoices.

## User flow

1. Open **SHVYA Sales** from the dashboard sidebar.
2. Create a quotation, agreement or invoice.
3. Choose an organization-owned template and optionally link a CRM lead.
4. Add content and line items. Financial totals are recalculated on the server.
5. SHVYA freezes the selected template, branding and message defaults into the
   document snapshot.
6. Open **Send** and choose **Email**, **WhatsApp**, or both.
7. Edit the email subject/body and WhatsApp message for this send.
8. Each channel creates its own delivery record. A failure on one channel does
   not resend or roll back the other channel.

## Templates

Templates are organization scoped and can customize:

- document type and numbering prefix;
- accent colour, header, footer, logo URL and authorised-signature URL;
- safe HTML document layout;
- email subject and body;
- WhatsApp message body;
- CRM and document merge fields.

Saved document HTML is sanitized. Script tags, form controls, event handlers and
unsafe inline CSS are removed.

Documents store a presentation snapshot. Editing a template later does not
rewrite previously created documents.

## Merge fields

Examples include organization.name, recipient.name, recipient.email,
recipient.phone, lead.name, lead.pipeline, lead.stage, lead attributes,
document.number, document.title, document.issue_date, document.valid_until,
document.due_date, document.total, document.content, items_table and
document.terms. Template syntax uses double curly braces.

## Delivery rules

### Email

Email uses the organization SMTP configuration already managed in Connect Hub.
The account must be enabled and have a successful connection test.

### WhatsApp

SHVYA Sales does not select an arbitrary connected number. It resolves the
single connected WhatsApp account linked to the CRM lead's current pipeline.

- WhatsApp API and Business App Coexistence use the Cloud API transport.
- Hosted Account uses the linked-device Hosted transport.
- For Cloud API / Coexistence free-form messages, SHVYA respects Meta's active
  customer-service window. Outside that window it blocks the free-form
  WhatsApp leg rather than bypassing policy. Email can still be sent
  independently.

## Public customer actions

A sent document has a UUID public token.

- Quotation: customer can accept or decline.
- Agreement: customer can record electronic acceptance with full name/email.
- Invoice: customer can view the immutable document.

Draft documents are not publicly accessible.

## Data isolation

Every template, document and delivery is explicitly scoped to one Organization.
Dashboard reads and writes filter by the authenticated CRM user's organization.
