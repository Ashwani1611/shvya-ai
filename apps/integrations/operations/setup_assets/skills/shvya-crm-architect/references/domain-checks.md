# CRM architect domain checks

## Evidence
Read pipelines/stages/attributes, descriptions/order/active state, qualification mappings, Workflow dependencies, required fields and configuration dependency graph.

## Known traps
- Same label does not guarantee same semantic purpose; same purpose does not require a duplicate record.
- Renaming/deleting a stage can break qualification, Workflows, reporting and historical interpretation.
- Attribute display name is not a safe attribute key.
- Changing attribute type/options can invalidate existing values and mappings.
- Protected SHVYA stages must not be repurposed to bypass qualification.
- Empty stages may be valid future process states; volume alone does not prove they are useless.

## Verification
Validate organization/integrity after changes. For retirements inspect dependencies first, prefer archive, and confirm dependent qualification/Workflows still resolve.

## Handoffs
Qualification-specific mappings/target logic go to qualification. Lead-specific state correction goes to lead repair. Automation behavior goes to Workflow/Cadence skills.
