# Hosted group operations

## Read

list_hosted_whatsapp_groups takes an exact tenant-owned whatsapp_account_id and bounded limit. read_hosted_whatsapp_group takes that ID, exact returned group_id and bounded limit. These are Hosted group tools, not a general WhatsApp provider API. The group belongs to a currently permitted account/session; names are never accepted as send targets. Verify active connection and ownership through the returned context/schema.

Read tools provide bounded text/history without downloads or read receipts. Missing attachments and pagination/window bounds limit conclusions. Avoid repetitive polling. A session failure, provider exception or permission denial is not proof a conversation is empty. Do not retry through a different account to bypass access.

## Exact send

send_hosted_whatsapp_group_message takes whatsapp_account_id, sender_member_id, group_id and body plus canonical write fields. Sender must be the actual member owner of that Hosted account. The backend prefixes the member's name; show the final prefixed content from dry-run in the approval preview. This differs from the historical Kraya description of unmarked member messages.

Every send requires a fresh exact per-message dry-run receipt, scoped to actor/tenant/tool/message/group/sender. A changed body, different group, different sender or drifted context requires a new preview. Existing user authorization can confirm the exact message, but never waive the server receipt. One approval cannot send a batch. The backend uses an idempotent provider request; preserve its receipt and inspect uncertain outcomes before retrying.

Only plain text is documented here. No file send, edits or deletion are assumed. Returned accepted/queued state is not a device delivery receipt. Never share confidential content from another company, credentials, portal tokens or unapproved promises.

## Export fallback and related evidence

analyze_setup_group_export consumes supplied authorized JSON/text normalized to its schema; it does not connect to WhatsApp. get_conversation reads a known tenant lead conversation, not an arbitrary group. Keep these evidence sources distinct. If live access is missing, use an authorized supplied export or report the exact unavailable capability.
