# Voice agent domain checks

## Evidence
Use approved company facts, Playbook/qualification rules, languages, call objective, consent/contact context and the selected provider's documented runtime capabilities. Current SHVYA MCP voice work remains artifact-only unless live provisioning/calling tools are actually exposed.

## Known traps
- A prompt saying “end call”, “book”, “send link” or “update CRM” does not create that backend capability.
- Provider voice/model/version IDs and pause syntax are provider/version-specific.
- Pronunciation/language behavior must be tested; bilingual assumptions are not universal.
- Transcript text can omit audio nuance and is not proof a backend action succeeded.
- Publishing a prompt is not proof the live agent is using that version.
- Lead context should be minimized and sourced; stale CRM data must not override explicit caller corrections.

## Verification
Produce agent policy + call instructions + outcome contract + test matrix. For eventual provider integration require version/read-back evidence, terminal action support, opt-out/handoff behavior and representative calls before marking operational.

## Handoffs
Qualification policy → qualification; approved facts → AI Brain/knowledge; live provider lifecycle → integration manager/engineering.
