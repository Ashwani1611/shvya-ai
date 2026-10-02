# Workflow builder domain checks

## Evidence
Read live trigger/action schemas, existing Workflows, CRM IDs/options, Cadence/channel dependencies, scopes/conditions, enabled state and representative simulation inputs.

## Known traps
- A valid schema does not mean the Workflow is reachable from real events.
- Enabled does not prove it has ever fired.
- Multiple overlapping Workflows can create duplicate/conflicting actions.
- Source attribute and canonical lead source are not interchangeable.
- Stage moves can create loops or conflict with qualification authority.
- A downstream Cadence/template/account may be invalid even when Workflow validation passes.
- Rule order should not be assumed to provide suppression unless backend semantics guarantee it.

## Verification
Validate configuration, simulate matching and non-matching events, inspect overlap/dependency graph, apply disabled when appropriate, read back, then activate only after acceptance conditions are met.

## Handoffs
Cadence action → Cadence builder; routing/send action → channel routing/provider skill; qualification stage logic → qualification.
