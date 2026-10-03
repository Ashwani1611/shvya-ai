# Hosted manual reply delivery

Manual Hosted inbox text, photo, video and document sends persist a WhatsAppMessage row and immediately call the Hosted sender in the web request. They no longer publish their initial delivery to Celery. The endpoints are non-atomic requests, so the short row claim commits before gateway I/O.

Only persisted, server-labelled `shvya_hosted.origin=agent` outbound messages without AI, welcome, follow-up, workflow or sales markers qualify for the manual fast path. They do not wait for the application automation admission budget. AI's shared 45-second send gate, Account Health automation protection, Meta API transport and background automation scheduling are unchanged.

The row progresses through queued (durable creation), sending (claimed), then provider-confirmed sent/delivered/read or a visible failure. The API returns the refreshed persisted status rather than the pre-send queued object. A queued database row is not a promise that WhatsApp sent it. Sending and final state changes publish inbox refreshes.

Provider throttling, a busy gateway or a pending acknowledgement can still require a retry. Only those conditions publish a background retry using the same message/request ID and the existing bounded retry budget. An uncertain outcome or payload conflict is not blindly resent. If retry publication fails, the message becomes visibly failed rather than remaining silently queued. WhatsApp connectivity and network latency still determine actual delivery time.

Regression coverage: `apps/channels/tests/test_hosted_manual_immediate.py` (20 tests, including media and status subcases), plus updated transport-separation and unresolved-LID inbox tests. Run with the Hosted send scale-safety, AI send-gate and account-health suites before release. Mocked gateway tests do not substitute for a live connected-account smoke test.
