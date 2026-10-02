# Qualification domain checks

## Evidence
Read complete qualification config, canonical Playbook qualification sections, current pipeline/stages, mapped attributes/options, affected lead evidence and execution/diagnostic state when troubleshooting.

## Known traps
- AI saying “qualified” is not backend completion.
- A stage move does not prove attributes persisted; inspect canonical action/evidence.
- Existing CRM values may be stale and can be corrected by newer explicit customer answers.
- Multiple inbound messages can create ordering/retry effects; do not infer answer order from storage order without trace evidence.
- Option letter/value normalization must preserve context.
- A structured qualification upsert can regenerate qualification-owned Playbook sections.
- Refusal/ambiguous answers are not valid required evidence.

## Verification
Compile/validate, simulate positive/negative/correction branches, read back mappings/target, then test representative lead behavior. Completion must be authoritative before repair to Qualified.

## Handoffs
Playbook prose → AI Playbook; schema gaps → CRM architect; lead reconciliation → lead repair; runtime silence/queue issues → AI debugger.
