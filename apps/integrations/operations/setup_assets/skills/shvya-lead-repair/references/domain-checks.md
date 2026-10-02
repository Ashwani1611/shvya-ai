# Lead repair domain checks

## Evidence
Resolve exact tenant-owned lead; read snapshot, pipeline/stage, relevant attributes, qualification diagnosis, conversation/trace and transition/action evidence.

## Known traps
- One broken lead can be a systemic config/runtime defect; do not patch symptoms repeatedly.
- Stage text in an AI reply is not transition evidence.
- Cross-pipeline moves require real ownership/transition validation.
- Missing attribute after a conversation may reflect failed action, invalid mapping, async timing or newer correction.
- Bulk populations should not be hidden loops of single-lead writes.

## Verification
Dry-run the smallest repair, apply with approval, re-read the lead, rerun relevant diagnosis and confirm no broader dependency remains broken.

## Handoffs
Systemic qualification → qualification; schema → CRM architect; runtime/queue → AI debugger; broad incident → incident repair.
