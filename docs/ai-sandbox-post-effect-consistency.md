# AI Sandbox: final composition after preview effects

Follow-up to evidence recovery (#558), no-send comparison (#559), and source-bound
WhatsApp file outcomes (#561). This change belongs to the existing Playground
service and does not add another engine, action executor, transport or startup shim.

## Behavior

The Playground's existing language-only final pass now also runs after attribute,
reminder and guided-file previews, not only a stage transition. Attempted validated
actions and file selections which made no preview change also receive final context.
Plain replies and silence do not trigger an extra final pass.

Final generation receives the existing safe attributes/qualification context and
keeps the resulting reminder instead of replacing operational state with stage-only
metadata. The current source-turn identifier and actual preview event types form
`resolved_actions`; proposed actions alone cannot populate its applied action list.
No-op and rejected previews are described as `not_applied`, not as confirmed failures.

A file card is `preview`, never a sent/delivered receipt. Final candidate rebuilding
sees the retained evidence before filtering to the already selected file. Successful
and fallback decisions cannot select a different file or introduce CRM/qualification
updates. Preview files still use the existing tenant-scoped eligibility/download path.

The complete in-memory visitor state is restored after final composition, including
reminder, stage/pipeline references, shared-file IDs and JSON attributes. An exception
restores state before fallback runs. The provider, context builder and existing
ContextVar flag are restored on exit. This is not a transaction around arbitrary
ORM writes; no-send guarantees still depend on the existing Sandbox boundaries.
The final-composition method rejects persisted leads or another organization's visitor.

Playground records its last-asked requirement from the final displayed decision,
not from the earlier draft. Existing qualification authorization and validation remain
responsible for selecting a permitted next question.

## Tests

```sh
python -m unittest apps.ai_engagement.tests.test_playground_finalization_contract -v
pytest apps/ai_engagement/tests/test_playground_post_effect_response.py \
       apps/ai_engagement/tests/test_sandbox_api_engine.py
pytest apps/ai_engagement/tests apps/channels/tests
```

Contracts test the pure projection/state helpers. Django regressions exercise the
real graph with mocked providers, along with error-isolation tests of the actual
final-composition method. Preview channels cover Sandbox, WhatsApp and Instagram;
these are not independent API/coexistence/Hosted transport or live-model tests.

## Limits and remaining work

At most one post-effect final pass is admitted per Playground turn. The existing
15-second admission check and five-second default final-provider timeout remain;
they are not a hard total latency or cost cap. When the admission window has elapsed,
the existing skip behavior remains. Existing model generation/grounding limits and
safety checks are unchanged, including language and fallback handling.

A live no-send OFF/ON comparison still requires authenticated runtime access and
verified internal scenarios. The deployed recovery feature's enable flag and explicit
organization allow-list are unchanged by this patch. No real CRM mutation, customer
message, source repair or per-organization activation is performed by these helpers.
Live Instagram delivery parity, broader CRM action receipts, source repair, incremental
cost reporting and selective checkpointing remain separate work.

## Follow-up implementation

See `ai-engagement-consistency.md` for the receipt-backed live action context,
Instagram source-bound outcomes, bounded final correction, operator source repair
and exact reservation-based credit reporting. Earlier remaining-work notes above
describe the scope of that original patch; live activation and real-recipient
verification are still separate from implementation and CI.
