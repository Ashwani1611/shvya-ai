# SHVYA Sales

> **Current-reference refresh:** verified on 2026-10-12 against `main` commit `24f2a1d5f75d`. Source code, Django models/migrations, tests, and runtime configuration remain the executable source of truth.

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

## Sales template branding through Operations MCP

The actor-bound Operations MCP exposes tenant-scoped Sales template tooling for authorized Superadmins: `list_sales_templates`, `upsert_sales_template_branding`, `upload_sales_template_asset`, and `attach_vault_asset_to_sales_template`. The Vault attachment path copies an approved private image into Sales-owned storage rather than exposing a private Vault URL. Writes remain gated by `sales.template.write`, explicit organization context, validation, audit and any applicable approval/stale-state checks.

## CRM and tenant rules

- Documents, templates, payments and deliveries are organization scoped.
- Lead relationships must belong to the same organization.
- Public document tokens expose only the intended customer document surface.
- Sending through WhatsApp must still follow the lead's valid pipeline-bound sender rules.
- Email/WhatsApp provider success is recorded as delivery evidence; a UI click alone is not success.
- PDF hashes and revision metadata are retained as document evidence.
