# Context and delegation contract

Use delegation to reduce context overload, not to multiply authority.

## When to fan out

For large setup/review tasks, independent read-only slices may be delegated in parallel when the host supports it, for example:
- business/intake extraction;
- CRM/configuration inventory;
- call/group requirement extraction;
- AI/knowledge conflict audit;
- production-behavior sampling.

Give every delegate the same verified organization identity, scope/time window, evidence limitations and an explicit output axis.

## What delegates return

Return discrete checkable findings with:
- source/reference and time;
- claim;
- confidence/coverage limit;
- required follow-up;
- no live mutation.

Delegates do not select tenants, open/close support context, approve writes or mutate shared state.

## Merge rules

- Merge by business requirement or root cause, not by source.
- A missing slice becomes a targeted follow-up or UNKNOWN, never a guess.
- Contradictory reports are resolved by opening the authoritative source; never average them or pick the more confident wording.
- Two delegates reading the same upstream summary are not independent corroboration.
- Collapse duplicate symptoms into one producing-layer root cause before reporting.

## Mutation serialization

Do not run dependent tenant writes in parallel. Create/read-back upstream resources before using their returned IDs downstream. Parallelize only truly independent drafts/reads where shared context cannot race.

## Context budget

Load the smallest relevant skill/reference set. Respect truncation and progressive retrieval. Keep an execution ledger of decisions, IDs, approvals and unresolved gaps in the coordinating agent.
