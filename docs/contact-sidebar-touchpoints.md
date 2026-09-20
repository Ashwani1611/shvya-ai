# Contact sidebar and Touchpoints

WhatsApp API and Meta Coexistence share the API inbox. Instagram uses the same independently loaded contact panel for linked CRM leads. Hosted linked-device WhatsApp is a separate inbox and is unchanged.

- The panel loads from `chat-contact-panel` after the message surface renders. Reloads of the current WhatsApp thread retain the panel, tab, scroll position, and message draft. New lead navigation flushes pending autosave requests and rejects stale navigation responses.
- Name, email and configured attributes autosave through the existing tenant-scoped CRM endpoints. Saves are serialized and bound to the original lead URL. Phone and source are read-only. Pipeline and stage changes use the shared CRM transition service and respect required stage attributes.
- Intent Score comes from `intent_score_for_lead`. Sequence display comes from `LeadSequenceState`, not an attribute string. Notes and calls use existing CRM persistence and activity logic.
- Checking In lists active sequences for the lead pipeline's linked, connected API sender. Starting a sequence calls `assign_sequence`; normal timing, business hours, pauses and account automation settings still apply. WhatsApp templates list all statuses for that sender; only approved templates can be sent. The send endpoint also enforces the sender boundary.
- Instagram does not send WhatsApp templates or trigger WhatsApp sequences. Its own composer retains Instagram policy and idempotency checks. Unlinked conversations show the existing CRM linking action.
- Cadence > Touchpoints is the shared organization-wide saved reply library. Categories contain any number of replies. The panel reads the same models. Replies are inserted into the active composer for review and normal sending; there is no alternate provider send path. Deleting a category cascades to its saved replies only, never to sent messages.

## Release

Migration `followups.0005` creates the category and reply tables and the organization/category uniqueness constraint. Deploy through successful main CI after the migration is available. No secrets or additional services are required.

## Validation

`apps/channels/tests/test_contact_panel.py` covers tenant isolation, CRUD, locked fields, pipeline/stage boundaries, linked template rejection, and real sequence assignment. `tests/browser/test_contact_panel_browser.py` uses real templates and scripts with intercepted network traffic to check drafts, autosave, latest-navigation-wins behavior, and routing writes. These tests never contact providers or send customer messages.
