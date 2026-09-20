# Contact sidebar and Touchpoints

WhatsApp API and Meta Coexistence share the API inbox. Instagram uses the same independently loaded contact panel for linked CRM leads. Hosted linked-device WhatsApp uses the same panel while retaining its separate message transport and live inbox.

- The panel loads from `chat-contact-panel` after the message surface renders. Reloads of the current WhatsApp thread retain the panel, tab, scroll position, and message draft. New lead navigation flushes pending autosave requests and rejects stale navigation responses.
- Name, email and configured attributes autosave through the existing tenant-scoped CRM endpoints. Saves are serialized and bound to the original lead URL. Phone and source are read-only. Pipeline and stage changes use the shared CRM transition service and respect required stage attributes.
- Intent Score comes from `intent_score_for_lead`. Sequence display comes from `LeadSequenceState`, not an attribute string. Notes and calls use existing CRM persistence and activity logic.
- Checking In lists active sequences for the lead pipeline's linked, connected API or hosted sender for the current conversation. Starting a sequence calls `assign_sequence`; normal timing, business hours, pauses and account automation settings still apply. WhatsApp templates list all statuses for that sender; only approved templates can be sent. The send endpoint also enforces the sender boundary.
- Instagram does not send WhatsApp templates or trigger WhatsApp sequences. Its own composer retains Instagram policy and idempotency checks. Unlinked conversations offer inline Create lead with an explicitly confirmed phone.
- Cadence > Touchpoints is the shared organization-wide saved reply library. Categories contain any number of replies. The panel reads the same models. Replies are inserted into the active composer for review and normal sending; there is no alternate provider send path. Deleting a category cascades to its saved replies only, never to sent messages.

## Release

Migration `crm.0028` adds the persistent lead auto-follow-up preference and preserves active/paused assignments that were already disabled. Migration `followups.0005` creates the category and reply tables and the organization/category uniqueness constraint. Deploy through successful main CI after the migration is available. No secrets or additional services are required.

## Validation

`apps/channels/tests/test_contact_panel.py` covers tenant isolation, CRUD, locked fields, pipeline/stage boundaries, linked template rejection, and real sequence assignment. `tests/browser/test_contact_panel_browser.py` uses real templates and scripts with intercepted network traffic to check drafts, autosave, latest-navigation-wins behavior, and routing writes. These tests never contact providers or send customer messages.

## Lead controls and hosted chats

- Collapse/expand keeps a slim, keyboard-accessible rail and remembers the choice for the browser session. On smaller screens the expanded panel overlays the right edge and can be collapsed to return space to messages.
- Checking In exposes `Lead.auto_followup_enabled`. Turning it off synchronizes assignment flags, excludes the lead from dispatch, and is checked again at execution (including hosted transport retries). `assign_sequence` locks and re-reads the lead, refusing assignment while disabled. Re-enabling does not reset sequence progress or override account/pipeline timing rules.
- API/Coexistence orphan messages appear in the inbox with Create lead. The phone comes from the persisted account-scoped message, never from editable request input. Hosted creation resolves the existing inbox identity and rejects groups/unresolved LIDs. Creation uses an accessible pipeline linked to the current sender, links history, avoids duplicate contacts, and does not send a welcome message.
- Hosted Checking In lists only sequences created for the exact linked hosted account. API sequences and other hosted-account sequences are excluded, with the same constraint enforced when starting a sequence. Hosted templates are unavailable; Touchpoints works through the existing hosted composer.
