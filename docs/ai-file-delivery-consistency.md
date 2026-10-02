# AI file delivery and source-bound response consistency

Follow-up to AI Brain recovery (#558) and no-send validation (#559). This is a
narrow correction of existing file/action projections, not a new sender, graph,
database, webhook processor or retry engine.

## Ownership and behavior

- WhatsAppMessage remains the authoritative transport record. `sent` means
  provider acceptance, not confirmed recipient delivery. `delivered` and `read`
  are used only when present in persisted transport state or the same message's
  previously recorded acknowledgement.
- Send-task result strings cannot elevate a queued message or invent a read
  receipt. API/coexistence and Hosted keep their existing send boundaries.
- A delayed outcome for source A updates A's receipt/history, never source B's
  current file selection. File and CRM-action projections cannot inherit another
  source's runtime markers. Explicit empty selections/action lists stay empty.
- File projections validate organization, lead, document and outbound/source
  binding. Account ownership is checked without overriding the existing sender
  routing policy; a permitted pipeline transition can change the sending account.
- Delivery/read acknowledgement does not regress on late send/failure results.
  `sent` may become `failed`; a newer successful attempt may supersede an older
  actually failed attempt. This code never creates that retry or resends a file.
- Duplicate outcomes preserve first-recorded sent timestamps and bounded history.
  Malformed legacy history entries cannot abort receipt persistence.
- Reconciled file status is refreshed from its source-bound outbound row when
  read, rather than trusting an old pending/sent snapshot. This does not create
  additional provider calls. Missing/mismatched delivery evidence stays uncertain.
- The existing English action-claim guard no longer changes failed/uncertain file
  claims into "I'm sending" or reauthorizes that attempt. Multilingual semantic
  checks remain the existing grounding policy; this is not a new language judge.
- A receipt-projection database outage is isolated after transport execution and
  does not turn an already executed provider send into a task failure. Canonical
  transport records remain available for diagnosis and subsequent reconciliation.
- Provider error bodies are not duplicated into the new action projection.
  Existing message error records remain unchanged.

## Verification

Credential-free contracts:

```sh
python -m unittest apps.ai_engagement.tests.test_file_delivery_receipt_contract -v
```

Database/runtime regressions:

```sh
pytest apps/ai_engagement/tests/test_file_delivery_receipt_integration.py \
       apps/ai_engagement/tests/test_canonical_architecture.py \
       apps/ai_engagement/tests/test_transactional_decision_reuse.py
```

The change must pass the affected AI/channel CI suites. Unit/database tests do
not prove real recipient delivery, concurrent worker behavior under production
load, or live-model response accuracy. This correction does not enable
AI_BRAIN_RECOVERY_ENABLED or change any organization allow-list. It adds no
migrations or deployment settings.

## Remaining rollout work

Run the no-send readiness and OFF/ON comparison documented in
`ai-recovery-validation.md` inside an authenticated internal-organization runtime.
Then verify real test-recipient delivery separately. Complete broader Playbook
and Sandbox post-effect consistency, Instagram outcome parity, cost reporting
and source-repair work in separately tested changes. Checkpointing stays deferred.
