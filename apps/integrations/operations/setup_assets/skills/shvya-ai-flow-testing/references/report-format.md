# Test report format

Start: Cleanup verified / Cleanup incomplete, with run IDs and remaining fixture status.

State the exact organization, configuration revision/date, engine/harness, model authority, simulated channels and tested evidence level. List task cap, actual runs, user input turns, AI replies, provider calls, reserved/used credits, elapsed time and retry counts from server records. Do not infer spend when unavailable.

Provide a results table: requirement, scenario/run, executed turns, conversational result, saved-state result, evidence reference, severity and reruns. Follow with verified defects, intermittent failures, working behaviors and prioritized repairs. Include budget-limited/skipped cases and source/access limits.

End with a coverage matrix and the precise readiness boundary: production-engine no-send results do not verify device delivery, channel sessions, provider template approval, webhooks, inbox realtime updates or independent worker scheduling. Record who must perform any remaining separately authorized live test.
