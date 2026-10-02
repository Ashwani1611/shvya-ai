# AI Brain domain checks

## Evidence
Read complete About, Playbook, languages, qualification, FAQs, knowledge health/source state, relevant channel settings/team handoff and representative production trace when behavior is questioned.

## Known traps
- Knowledge present is not knowledge ingested/published/retrieved.
- A generic fallback can originate outside the model prompt; identify the producing code/runtime layer before editing Playbook.
- Duplicating business facts across About/Playbook/FAQ/files creates contradiction risk.
- A Playbook instruction cannot make unsupported backend actions real.
- Strong config can still fail because of routing/queue/provider/runtime.
- A single correct Sandbox answer does not prove production channel parity.

## Conflict audit
Check fact ledger, promises without material, instruction conflicts, stale examples, unwired channels/resources, and lead-facing copy that invites unsupported answers.

## Verification
Run policy tests/conversation simulation, then production trace on reported incidents. Separate grounded answer quality from action execution and delivery.

## Handoffs
Playbook structure → AI Playbook; source lifecycle → knowledge manager; question/mapping → qualification; runtime failure → AI debugger.
