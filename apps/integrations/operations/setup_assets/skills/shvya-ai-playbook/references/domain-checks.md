# AI Playbook domain checks

## Evidence
Use full current Playbook, qualification contract, CRM definitions/options, approved facts/knowledge metadata, channel constraints and current business rules.

## Known traps
- Replacing from a truncated Playbook can delete unrelated policy.
- Factual business content duplicated into Playbook drifts from knowledge.
- Display labels are not safe IDs/attribute keys.
- Prompt instructions cannot override consent, routing, qualification or provider limits.
- Customer-visible text and private instructions must remain structurally separated.
- Unsupported placeholders or unresolved authoring variables can leak literally.

## Verification
Canonical parser/qualification compiler, diff against existing sections, dry-run/save/read-back, policy simulation and representative conversation tests.

## Handoffs
Facts/sources → AI Brain/knowledge; qualification schema → qualification; CRM definitions → CRM architect.
