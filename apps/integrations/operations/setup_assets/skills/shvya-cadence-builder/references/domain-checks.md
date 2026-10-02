# Cadence builder domain checks

## Evidence
Read Cadence provider/sender, active state, ordered steps/IDs, timing, approved templates, variables, pipeline/account routing, business hours, enrollment source, suppression/handoff rules and delivery history where relevant.

## Known traps
- Active Cadence is not proof any lead is enrolled.
- Changing provider/sender in place may be unsupported; migration needs a new Cadence.
- API template approval does not guarantee variables/media payload are valid.
- Free-text WhatsApp has service-window/channel constraints.
- Deleting/editing a step with delivery history can break audit/retry semantics.
- Reordering without stable step IDs can duplicate or lose steps.
- Timing should be interpreted against business hours and configured units, not prose assumptions.

## Verification
Create/reuse Cadence, add steps sequentially, verify contiguous order/active state, simulate timing, validate batch/dependencies, then verify enrollment path separately.

## Handoffs
Enrollment → Workflow builder; routing/templates → WhatsApp/channel routing; email sender → email skill.
