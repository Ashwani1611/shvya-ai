# 99acres lead integration (SHVYA Connect Hub)

## Overview

SHVYA supports 99acres property enquiries using the XML specifications supplied by the integration customer:

- **Push**: 99acres posts an XML \`<Xml><Qry>...\` batch to a private SHVYA webhook.
- **Pull**: SHVYA POSTs a form field named \`xml\` containing \`<query>\` (username, password, start and end timestamps) to a provider API, and parses nested \`<Resp><QryDtl>...\` and \`<CntctDtl>...\` elements.
- **Both**: Use both adapters with one idempotent enquiry receipt table. It requires agreement from 99acres about provider-generated query IDs.

These are distinct XML formats. External IDs are recorded once per organization, so repeated pushes, Pull overlap and provider retries do not create duplicate CRM enquiries.

## Provisioning

1. Organization admin opens **Dashboard → Connect Hub → 99acres** and selects **Request setup**.
2. SHVYA Superadmin opens that organization's overview → **99acres**.
3. Select the mode, the organization-owned pipeline and active initial stage.
4. For Push: choose **Generate & activate** to create a unique private HTTPS URL. Share the URL securely with the 99acres account manager to configure lead push. Rotate the URL if exposed.
5. For Pull: enter the 99acres account username, password and **the provider-specific opaque token** from the 99acres Pull API URL; all three are encrypted at rest. A Pull-only integration does not need a Push webhook.
6. Ask the provider to send a test query (Push) or select **Queue Pull sync** (Pull). Verify recent activity and the CRM lead source.
7. Do not activate customer-facing messaging until organization consent and template policies are verified.

**Never put a 99acres account password or endpoint token in source control, public docs, logs, a support ticket or a browser-visible form value.** SHVYA does not show decrypted credentials after saving.

## Ingestion and mapping

| Provider field | SHVYA |
|---|---|
| Push \`QryId\` / Pull \`QryDtl@TblId\` | Per-organization idempotency key |
| \`Name\`, \`Phone\`, \`Email\` | CRM buyer contact |
| \`QryInfo\`, \`CmpctLabl\` | Lead interest note / description |
| \`ProdId\`, \`ProdType\`, \`Project\` | Property context |
| \`QryType\` or \`ResType\`, \`RcvdOn\` | Enquiry metadata |

10-digit Indian numbers are normalized to +91. Other bare numbers need an explicit country code. The existing contact's name, pipeline, stage, source and email are preserved; every distinct property enquiry is appended as a separate system note. A new contact gets \`lead_source=99acres\` and does not automatically queue a WhatsApp welcome.

A successful receipt is retained as a tombstone if its CRM lead is deleted. Event diagnostics store status, query ID, direction and sanitized error code without buyer names/phones/emails.

## Rate limits and recovery

The supplied Pull document specifies up to **6 requests/hour**, a **maximum 2-day interval**, a **30-day lookback**, **5,000 records per response**, and **no pagination**. The scheduled worker polls approximately every 15 minutes, enforces a database hourly quota and a two-minute timestamp overlap. A cursor advances only when all records for that window are successfully processed; if the 5,000 result limit is hit, the connector fails closed to avoid missing leads.

Push XML batches accept up to 1,000 queries and a maximum request size of 2 MiB; Pull XML is bounded at 8 MiB. Both reject DTD/entity declarations.

## Provider contract still to validate

The supplied 99acres documents are old and contain inconsistencies:

- \`pwd\` vs \`pswd\` for Pull XML. The implementation uses \`pswd\` from the request samples.
- Two- versus four-digit year formats. The implementation uses \`YYYY-MM-DD HH:mm:ss\` in Asia/Kolkata.
- Push only explicitly documents status \`Y\` on successful per-query processing. SHVYA returns \`N\` for failed queries and HTTP 503 for a partial batch so the provider can retry. **Confirm failed-query ACK and retry semantics with 99acres** before enabling a live tenant.
- Confirm provider API URL token issuance, live Pull endpoint, limits, and Push delivery frequency with the account manager.
- Push/Pull query IDs might differ even for the same enquiry, and account-manager confirmation is required before relying on cross-mode deduplication.

## Testing

Use synthetic XML to exercise parser, dedup, input validation, organization isolation, webhook token rotation, Pull rate limit, and failure handling:

\`python manage.py test apps.integrations.tests.test_acres99 apps.integrations.tests.test_connect_hub\`

Before release run Django migrations and the standard repository CI, then complete a live-provider smoke test using a customer-approved test account. No 99acres credentials or customer information are included in the repository.
