# Live AI acceptance recovery

Post-deployment checks found that successful reply text alone does not establish
correct qualification, file sharing or transport state. This repair addresses
three independently reproduced gaps:

- Sandbox Brain recovery returned before the normal missing-answer and file
  reviewers. Recovered drafts now use those same bounded reviewers and retain
  the normal action validation and grounding gates. Safe diagnostics retain the
  original provider failure.
- An allowed qualification value could be supported by an exact quote that
  actually contradicted the value. Obvious option, binary and numeric-band
  conflicts are rejected; ambiguous and multilingual answers still require the
  existing semantic checks.
- Hosted sends waiting for a provider acknowledgement discarded the provider
  message ID. The authenticated acknowledgement callback consequently could not
  match the queued row. Pending IDs are now retained for correlation; they do
  not establish delivery. Confirmed acknowledgement state must survive timeout
  and retry races.

## Acceptance cases

Use internal recipients and isolate each receiving lead's AI control to prevent
assistant-to-assistant loops. Keep account and stage controls intact.

1. In a new test, volunteer four explicit qualification answers. Verify the
   exact mapped values and configured completion stage, without repeated
   questions.
2. Add a callback request with an explicit future date, time and timezone.
   Verify callback-stage priority and one persisted reminder. Sandbox must
   label these as previews and create no live reminder.
3. Request the approved brochure and ask a price question in the same turn.
   Verify the answer, file selection, recipient delivery and actual download.
4. During Hosted acknowledgement delay, verify the same message remains queued
   until an authenticated provider acknowledgement confirms it, then clears its
   temporary error. Retry identity and send pacing must remain unchanged.
5. Repeat through WhatsApp API, Hosted, Instagram and an identified official
   Coexistence connection. Browser UI presence is not proof of recipient delivery.

## Verification limits

Local regressions establish these contracts, not live model quality or provider
availability. An observed provider token-rate-limit error is a separate runtime
constraint. Missing inbound events, a truly undelivered message, Meta billing
errors and an unidentified Coexistence account cannot be marked resolved by
these source changes alone. Repeat live acceptance after deployment.
