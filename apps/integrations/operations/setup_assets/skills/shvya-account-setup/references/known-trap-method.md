# Known-trap method

Every domain skill has a `references/domain-checks.md` file containing its common false positives and edge cases. Read it before reporting a defect.

## Generic traps

- **Configured is not live.** Existence does not prove enabled, eligible, triggered or delivered.
- **Attempts are not impact.** Retries can make one lead look like dozens of failures.
- **Historical/imported traffic is not live acquisition.** Backfills and sync can inflate lead/message counts.
- **Current state is not incident-time state.** A repaired setting can make an old incident look impossible.
- **UI projection is not always canonical state.** Confirm critical findings through backend reads.
- **Provider connected is not operational.** OAuth/account state does not prove webhook subscription, routing or sends.
- **Queue row is not delivery.** Distinguish queued, claimed, executed, provider accepted and delivered.
- **Knowledge exists is not knowledge used.** Verify ingestion, publication, retrieval/grounding and response policy separately.
- **AI text is not backend action success.** Stage/attribute/reminder/file behavior requires canonical action evidence.
- **One identifier may have multiple modes.** WhatsApp API, Coexistence and Hosted have different capabilities and storage paths.
- **A prompt cannot override backend policy.** Qualification, routing, consent and tenant boundaries stay authoritative.

## Reporting rule

Before promoting a suspicious observation to a finding:
1. read the domain checks;
2. identify at least one alternate explanation;
3. inspect the evidence that distinguishes them;
4. label anything unresolved as UNKNOWN rather than choosing the dramatic explanation.
