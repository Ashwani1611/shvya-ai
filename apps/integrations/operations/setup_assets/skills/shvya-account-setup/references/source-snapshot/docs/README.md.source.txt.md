# Skill documents

One markdown file per skill. Each file contains the skill's `SKILL.md` verbatim (Part 1) and a digest of every reference file and script in that skill's folder (Part 2). The canonical sources live under `.claude/skills/<skill>/`; these copies are for reading in one place. Regenerate them with the assembler described at the bottom when a skill changes.

| Skill | What it does | Touches the client's Kraya account? | Document |
|---|---|---|---|
| `kraya-account-setup` | Build or change a Kraya account end to end over the REST API: intake, Client Profile, qualification spec, sequences, stages, attributes, rules, quick replies, bump-ups, calendar, Co-Pilot, user settings, catalog, Ops CRM commitments | Yes, after a confirmed proposal | [kraya-account-setup.md](kraya-account-setup.md) |
| `account-review` | Audit a live account: requirement coverage, activation, funnel reachability, live behaviour from production traces, hallucination surface, feature utilisation, hygiene | No; one task on the Ops CRM card at the end | [account-review.md](account-review.md) |
| `ai-flow-testing` | Run persona-driven conversations on disposable test leads through the production reply pipeline and judge them against a fixed rubric | Test leads only (created, then deleted from a manifest) and one user setting restored | [ai-flow-testing.md](ai-flow-testing.md) |
| `account-handover` | Derive the lead flow from configuration and publish the onboarding demo page with diagrams, tables, script and open questions | No | [account-handover.md](account-handover.md) |
| `kraya-vault` | Pull the client's content portal and write call facts, questions and call records back for the client | No (writes to the Vault) | [kraya-vault.md](kraya-vault.md) |
| `read-whatsapp-group` | Read a client group through an ops member's hosted WhatsApp session; send a message or file on a per-message yes | No (sends into WhatsApp from the ops number) | [read-whatsapp-group.md](read-whatsapp-group.md) |
| `ops-client-sweep` | Scan every client group or an Ops CRM filter, rank by a rubric, work each client with a confirmed reply | No (confirmed sends; optional Ops CRM tasks) | [ops-client-sweep.md](ops-client-sweep.md) |
| `dialnexa-voice-agent` | Build, tune, test, publish and roll back AI calling agents on Dialnexa; the `ai_call` sequence step content | Only the `ai_call` step, through the setup skill | [dialnexa-voice-agent.md](dialnexa-voice-agent.md) |

The agent's own standing instructions are `CLAUDE.md` at the repository root. The end-to-end explanation of how everything fits is in [`../kraya-account-agent-guide.md`](../kraya-account-agent-guide.md), and the endpoint-by-endpoint list of powers is in [`../kraya-api-powers.md`](../kraya-api-powers.md).

## Regenerating

The digests live in `_digests/<skill>.md`. Each skill document is `header + SKILL.md + digest`. To rebuild after editing a `SKILL.md`, concatenate again; the header lists the files in the skill folder with their sizes.
