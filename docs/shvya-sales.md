# SHVYA Sales

> **Implementation snapshot:** verified on 2026-09-23 against staging runtime commit `84013a4190cfa97644e0216a896fa4ecc59eaebd`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

SHVYA Sales is the CRM-linked customer-document workspace for **quotations, agreements and invoices**.

## Workspace

Authenticated routes live below `/dashboard/sales/`.

The staging workspace supports:

- Sales settings;
- document list/create/edit/detail;
- quotation, agreement and invoice templates;
- server-generated PDF download;
- email and WhatsApp delivery;
- attachments;
- agreement revisions;
- manual payment/refund recording;
- payment refunds and credit notes;
- recurring invoice configuration;
- payment-link creation;
- scheduled-delivery cancellation.

## Core model

`SalesDocument` belongs to an organization and can link to a CRM lead and template. It stores customer-facing document number, recipient snapshot, currency/dates, line items/totals, rendered content, PDF file/hash/generated time, revision chain, public-view evidence and acceptance/signing timestamps.

Document types are:

- `quotation`
- `agreement`
- `invoice`

A revision does not silently rewrite historical delivery evidence; current-version/revision fields preserve the lifecycle.

## Delivery and tracking

`SalesDocumentDelivery` stores channel, sender/recipient snapshots, provider message ID, sent/failed state and tracking timestamps. The current model supports delivered/opened/clicked/bounced evidence, counters and bounce reason.

Lifecycle support also includes tracked links, Sales activity, scheduled delivery, reminders and attachments.

## Payments and post-sale lifecycle

Current staging includes:

- `SalesPaymentGateway`
- `SalesPaymentCheckout`
- `SalesPayment`
- `SalesCreditNote`
- `SalesRecurringInvoice`

Provider credentials/webhook secrets stay encrypted or environment-bound and must never be surfaced in customer-facing output, generic diagnostics or Operations configuration exports.

## CRM and tenant rules

- Documents, templates, payments and deliveries are organization scoped.
- Lead relationships must belong to the same organization.
- Public document tokens expose only the intended customer document surface.
- Sending through WhatsApp must still follow the lead's valid pipeline-bound sender rules.
- Email/WhatsApp provider success is recorded as delivery evidence; a UI click alone is not success.
- PDF hashes and revision metadata are retained as document evidence.
