# AI recovery: controlled validation and question-level coverage

This follow-up to #558 preserves per-question evidence coverage for final
composition, distinguishes assessment-provider failures, and supplies a repeatable
no-send comparison through the existing `PlaygroundService`. It does not add a
second engagement engine, alter customer activation, or change channel delivery.

## Behavior changes

- `Coverage.parts` retains each question, supported flag and validated source IDs.
  Generation receives these advisory details alongside the existing runtime policy.
  Question text remains untrusted data, not new instructions, business evidence,
  qualification authority or permission for actions/files.
- The existing channel, lead source and Bot Languages are explicit recovery context.
  Existing Playbook rules are neither rewritten nor interpreted by a new rule engine.
- Trace summaries still contain only counts, bounded codes and source identifiers.
  They do not include the question text or reformulated query.
- Assessment outcomes distinguish `provider_configuration_error`,
  `provider_rejected`, `provider_temporary_error` and `timeout` from malformed
  verdicts or insufficient evidence. This does not introduce an additional provider
  retry owner. Generation and final grounding retain their existing error handling.
- Source-resolution database errors are isolated with savepoints for real Django
  organizations so a failed optional read does not poison the caller's transaction.

## Read-only readiness: no AI calls

Run on the intended application host with its normal environment:

```sh
python manage.py evaluate_ai_recovery --organization-id <internal-organization-uuid>
```

The JSON result identifies this process's effective recovery flag, configured AI
policy/language/model names, presence (never value) of a provider key, source/index
counts, failed/unready sources, and IDs of up to 20 completed documents with no
searchable chunks. It does not create missing OrgInfo records or repair sources.

This is a **current-process** check, not confirmation that every Celery worker has
identical environment settings. It inspects database metadata, not original file
bytes, provider availability, credit balance, authenticated channel sessions or
recipient delivery. Check relevant workers and channels separately.

## No-send paired comparison: explicit live-model/credit consent

Author a small local fixture using verified organization facts. Keep production
customer exports and transcripts out of the public repository. The following is a
schema example; replace the message and expected phrase with a real approved fact.

```json
{
  "version": 1,
  "cases": [{
    "id": "approved-fact-instagram",
    "channel": "instagram",
    "lead_source": "instagram",
    "turns": [{
      "message": "A specific question answered by this organization's AI Brain",
      "expect": {
        "contains_all": ["an approved answer phrase"],
        "excludes": ["the team would need to confirm"],
        "file_count": 0
      }
    }]
  }]
}
```

```sh
python manage.py evaluate_ai_recovery \
  --organization-id <internal-organization-uuid> \
  --scenarios /secure/internal-recovery-cases.json \
  --live --max-turns 8 --budget-seconds 120 \
  --output /secure/recovery-comparison.json
```

`--live` is mandatory for scenarios: calls consume the organization's normal AI
credits. No new API key or provider adapter is introduced. The fixture supports
`sandbox`, `whatsapp` and `instagram` preview channels, optional tenant-validated
stage IDs and lead sources, and up to six turns per case. WhatsApp previews do NOT
independently validate API, coexistence and hosted transports.

Expectations support literal `contains_all`, `excludes`, `event_types` (reminder,
attribute_updates, stage_transition), `file_count`, `stage` name, and `attributes`.
Attribute assertions inspect the in-memory Sandbox state, not CRM writes. File
assertions count eligible preview cards, not queued or delivered messages.

Each case runs with recovery OFF and ON in separate in-memory sessions. A scoped
ContextVar applies only to that organization's `_SandboxLead`; it cannot override
flags for a persisted CRM lead or change environment variables/organization settings.
This permits an internal no-send comparison before enabling customer traffic.

The runner detects changes to company configuration, Playbooks, FAQs, documents,
chunks (including embeddings), pipelines/stages, attributes and model configuration.
It stops on drift. This is hash-based drift detection, **not a database snapshot
lock**; use a quiet internal test organization. Fingerprinting is capped at 5,000
source/config rows and is intentionally outside the per-message production path.

## Reports and limits

Reports omit messages, answers, expected phrases, attribute values, source content,
file URLs and raw exception strings. A requested report is created with mode 0600
and exclusive creation: existing files/symlinks are not overwritten. Sandbox session
history is kept in process memory; existing application logging and AI-credit usage
records retain their ordinary behavior.

`comparison_valid` describes stable comparison conditions, not answer quality.
Configured model choices are part of the source/config fingerprint. Returned
model-label differences are reported separately, not treated as source drift:
replacing a baseline fallback with a model response is an expected recovery
outcome. Human review must account for path/model-label differences when
attributing any measured improvement.
`acceptance` separately records literal checks; an unscored turn is NOT a pass.
The CLI exits unsuccessfully for an incomplete/incomparable run or missing/failed
recovery assertions. `recovery_exercised` distinguishes turns that actually entered
coverage/retrieval from deterministic shortcuts. A false value means this run did
not demonstrate recovery behavior, even when an unrelated assertion passed.

The existing `live_model_evaluated` field means the run returned at least one
non-fallback model-labelled response. It is not an external audit of model execution
and must not be interpreted as live evaluation when tests mock the provider.
These are operator-authored acceptance checks, **not a semantic correctness judge**.
Naturalness, language quality, contradictions and unnecessary refusal rates still
require reviewing controlled conversations in Sandbox. Usage remains metered;
the follow-up reports exact incremental internal credits, not provider currency cost. See `ai-engagement-consistency.md`.

The default limit is eight combined OFF/ON turns (hard maximum 40); fixture size is
128 KiB and at most 20 input turns. The comparison time budget stops new turns, but
is not a hard end-to-end deadline or model-token/cost cap. Existing provider/task
limits still apply within each turn. Start with one case and inspect actual latency.

## Verification and remaining rollout

Run the new contracts and integration suite, then the affected AI/channel suites:

```sh
python -m unittest apps.ai_engagement.tests.test_recovery_validation_contract -v
pytest apps/ai_engagement/tests/test_recovery_validation_contract.py \
       apps/ai_engagement/tests/test_recovery_validation_integration.py
pytest apps/ai_engagement/tests apps/channels/tests
```

Repository CI results, recorded-provider tests, operator live comparison and real
channel delivery are separate evidence. Do not mark live tests completed because
CI passed. Customer activation still requires the explicit global enable flag and
organization allow-list described in `ai-brain-evidence-recovery.md`, consistent
worker configuration, satisfactory live comparison and controlled test recipients.

Still outside this patch: production activation, automatic re-indexing, a new
source-rule interpreter, the wider CRM action/delivery lifecycle upgrade, real
recipient tests, removal of every fallback, and persistent graph checkpoints.

## Follow-up implementation

See `ai-engagement-consistency.md` for the receipt-backed live action context,
Instagram source-bound outcomes, bounded final correction, operator source repair
and exact reservation-based credit reporting. Earlier remaining-work notes above
describe the scope of that original patch; live activation and real-recipient
verification are still separate from implementation and CI.
