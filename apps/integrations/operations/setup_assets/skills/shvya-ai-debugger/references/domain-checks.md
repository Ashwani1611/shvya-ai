# AI debugger domain checks

## Evidence path
Trace one representative event end to end: inbound/provider evidence → lead/conversation resolution → routing → AI eligibility toggles/delay/business hours → queue creation → worker claim → prompt/qualification assembly → knowledge retrieval/grounding → model result → parsed backend actions → outbound queue → provider acceptance/delivery.

## Known traps
- Many failed jobs may be retries for very few leads; count distinct impact.
- A queue row saying processing does not prove a worker claimed it.
- A correct model reply with failed delivery is not an AI-quality defect.
- Generic fallback text can be emitted by backend fail-soft code outside the model.
- Missing stage/attribute/reminder can be action validation/persistence failure even when reply text is correct.
- Playground/sandbox success does not prove production worker/routing parity.
- Current toggle state may differ from incident-time state.

## Verification
Identify the first divergence in the expected chain. After repair rerun the same lead/scenario or deterministic equivalent and verify both response and required side effects.

## Handoffs
Grounding/source → knowledge manager; Playbook → AI Playbook; qualification → qualification; routing → channel routing; worker/platform outage → incident repair.
