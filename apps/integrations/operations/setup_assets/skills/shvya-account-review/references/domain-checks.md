# Account review domain checks

## Evidence axes
Review separately: requirement coverage, activation, reachability, observed behavior, ambiguity/conflicts, feature utilization and operational hygiene. Keep configured? and live/observed? as separate columns.

## Known traps
- Health/usage indicators do not prove configuration correctness.
- Retry storms inflate failures; quantify distinct affected leads/conversations and eventual recovery.
- Imported/historical messages can inflate activity without representing live acquisition.
- Current config may differ from incident-time config.
- A wrong answer present in Playbook/FAQ/knowledge is not automatically a model hallucination.
- Two sub-reports using the same source are not independent corroboration.
- Missing group/call evidence is UNKNOWN, not “no requirement”.

## Verification
Every defect must name the producing layer and supporting source/time window. Include what works with the same rigor. Before reporting, run the domain trap pass and collapse duplicate findings to root causes.

## Handoffs
This skill remains read-only. Route fixes to the owning domain skill; use `shvya-incident-repair` only after the review establishes a bounded operational incident.
