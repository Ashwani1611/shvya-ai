# WhatsApp template media delivery

WhatsApp API and Coexistence share the Cloud API template sender. Inbox, Cadence,
and Bulk Campaigns now build message-time header, body, button and carousel
parameters from the approved definition. Named parameters retain parameter_name;
numeric body parameters remain positional. Missing required values block sending.

A template approval upload handle is not a message-time media ID. Newly uploaded
header/card files are retained in the configured private media storage, referenced
by organization/account/template-scoped metadata. Workers upload them to the
sending number's /media endpoint and cache the resulting ID for one hour. They do
not put private storage paths or internal asset markers into Meta message payloads.

Bulk Campaigns selects existing template media automatically. An organization
admin can upload or replace the attachment in Personalization without entering a
URL. This saves the attachment for future broadcasts and sequences. Replacing
media invalidates reviewed campaign snapshots; review/create those campaigns
again before sending. The consent and exclusion confirmations remain required.

For older templates, a usable HTTPS sample returned by Meta can be used directly.
Opaque approval handles are never treated as IDs. If no usable media exists (or
a remote sample expires), upload the file once in Personalization. SHVYA cannot
recover file bytes that were not previously retained.

Deployment requires channels migration 0019_template_delivery_media and refreshed
static assets, web and worker processes. Apply to staging first. Validate one
text-only and one image-header template for both an API and a Coexistence number,
then a consented test broadcast and Cadence step. Do not use customer audiences
for release verification. Meta failures retain provider codes/details for the
existing delivery diagnostics.

## Recheck hardening

- Preview, confirmation and dispatch enforce each lead's current pipeline sender.
  Legacy Meta phone-number-ID bindings and formatted phone numbers remain supported.
- Template media stays attached to its carousel card when drafts are reordered.
  Changing sender accounts clears account-bound samples and attachments.
- Attachment preparation failures are distinguished from uncertain /messages
  requests. A response without a provider message ID is never recorded as sent.
- Cadence uses the same transport as Chats and pauses on unconfirmed sends rather
  than automatically replaying a potentially accepted message.
- Approved templates have an admin-only Sending setup screen to persist CRM
  parameter mappings/fallbacks and upload media. This supports synced positional
  parameters without inferring values from Meta's sample customer data. Campaigns
  start with these mappings and can customize them for their reviewed audience.
- A changed template/attachment requires campaign review again. Consent and
  exclusions are still explicit before Send now becomes available.
