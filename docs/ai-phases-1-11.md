# AI reliability phases 1–11

This extends the existing SHVYA AI runtime. API/Coexistence and Hosted still use
one qualification/policy/CRM core with their existing account-scoped delivery
owners. There is no model upgrade, new agent, or additional extraction model.

## Runtime ownership

Inbound tenant/account resolution → permission checks → isolated turn/AI Trace →
organization runtime profile → existing intent and qualification resolution →
evidence/semantic+keyword retrieval → backend conversation policy → structured
action proposals → locked canonical CRM execution → reconciled state → controlled
response composition/final grounding → existing transport queue → accepted-turn
memory/signals → trace completion.

The existing transactional two-pass path is retained: state-changing drafts may
propose actions; the post-state pass is language-only and its normalization guard
removes action/qualification mutations. A response plan does not grant database,
tenant, booking, handoff, or file-delivery authority to the model.

## Audit corrections

* Both unsafe grounding shortcuts now delegate to one conservative implementation
  in `grounding_safety.py`. Only a complete verified evidence item or an exact,
  finite rendering of a scalar price can skip independent verification. Word
  overlap, digit overlap, substring inclusion, and reordered negations are not
  accepted as grounding. Action confirmations/credential disclosures cannot use
  this shortcut. Ordinary acknowledgements have a small language-only allowlist.
* `AIActionReceipt` makes the existing planner keys durable. A source-bound action
  and its receipt commit in the same transaction. The lead row lock serializes
  concurrent attempts; database uniqueness provides defense in depth. Failed
  receipts roll back actions. Replays return the original result, without another
  note/reminder. Trusted non-message backend calls remain distinct operations.
* Every installed executor wrapper forwards `source_message`; explicit sources
  are tenant/account/lead validated. Targets and configured action permissions
  are rechecked against current state under the execution lock.
* An internal-label filter no longer removes an exact backend-authored next
  question when a normal business answer continues qualification. Only the exact
  authored trailing question/acknowledgment is exempt—not arbitrary model text
  following a blank line.
* Task-local intent/evidence/memory observations are reset at the API and Hosted
  execution boundaries, including failure paths. Existing classification results
  are reused within the same source-bound turn; already accepted replay sources
  need no new classification call.
* Trace string sanitization also redacts credential-like content inside free text.
  Memory trace mutations contain metadata, not raw old/proposed fact values.

## Phase implementation map

| Phase | Canonical implementation |
| --- | --- |
| 1: Observability | Existing `AITrace`, trace service/runtime and organization-filtered UI; additional objection/signal/response-plan sections. |
| 2: Intent | Existing generic deterministic-first `IntentEngine`; bounded existing model fallback, reused per turn. |
| 3: Policy | Existing `ConversationPolicyEngine`; configured continuation, call/human/opt-out precedence retained. |
| 4: Tenant/runtime | Existing `OrganizationAIRuntimeProfile` and `TenantGuard`; organization-configured action families and source validation. |
| 5: Evidence | Existing resolver + canonical semantic/hybrid context; retrieved IDs are reloaded under tenant/active-document filters, not trusted as arbitrary supplied text. Missing live availability stays unknown. |
| 6: Memory | Existing Lead-owned structured-memory JSON extended with bounded reported events and explicit customer facts. Existing conversation summaries remain separate. |
| 7: Planner | Existing `ActionPlanner` + canonical executor; transactional `AIActionReceipt`, full-decision/file/handoff trace observations. |
| 8: Objections | `sales_intelligence.ObjectionEngine`: generic categories, organization phrases/strategy/approved facts/offer/discount/escalation/forbidden claims. No invented rebuttal facts. |
| 9: Signals | `LeadSignal` individual source-bound observations, configurable deterministic weights/caps; `signal_summary()` exposes explainable components for Insights/Workflows. Never overrides qualification. |
| 10: Evaluation | Declarative scenarios and `evaluate_ai` command, plus existing transport/qualification/tenant tests. CI retains its full suite and adds a categorized evaluation report. |
| 11: Composition | `response_composer.ResponsePlan` augments (does not replace) the existing qualification execution plan. Facts, question, intent, language, tone and backend authority are supplied to the same response model. |

## Organization configuration

All options use the existing `Organization.settings` JSON; they are not global
SHVYA business rules. Existing organizations preserve their current qualification
flow unless the relevant optional setting is explicitly enabled.

```json
{
  "ai_action_permissions": {
    "allowed_action_types": [
      "attribute_updates", "pipeline_transition", "add_note",
      "create_reminder", "contact_updates"
    ]
  },
  "ai_qualification": {
    "capture_multiple_answers": false,
    "continue_after_answer": false
  },
  "ai_memory": {
    "enabled": true,
    "field_mappings": {
      "budget": "organization_existing_budget_attribute_key",
      "current_tools": "organization_existing_tool_attribute_key"
    }
  },
  "ai_objections": {
    "enabled": true,
    "persist_memory": false,
    "strategy": "Use only approved facts; acknowledge the concern.",
    "forbidden_claims": ["guaranteed results"],
    "categories": {
      "PRICE_TOO_HIGH": {
        "phrases": ["outside our range"],
        "strategy": "Explain the approved payment options.",
        "approved_facts": ["REPLACE with an actual organization-approved fact"],
        "escalate": false
      },
      "TRUST_CONCERN": {"escalate": true}
    }
  },
  "ai_signals": {
    "enabled": true,
    "weights": {"asked_pricing": 15, "requested_demo": 25},
    "caps": {"asked_pricing": 1, "requested_demo": 1}
  },
  "ai_response": {"tone": "Professional, warm and concise"}
}
```

Do not copy the placeholder approved fact into a real organization. `offer` and
`discount` may be supplied under a category only as explicit, truthful approved
text. An absent category never invents a discount or guarantee.

Absent action configuration preserves the existing supported action families;
an explicitly empty or malformed allowlist fails closed. Optional compound answer
capture uses explicit authored option values/anchored daily volumes, the existing
qualification validator, and exact configured attribute mappings. It does not
interpret an unasked single-letter option or use an unrelated price as volume.

Memory uses organization + lead identity, never phone alone. CRM and validated
qualification retain higher authority than inferred facts. Events are bounded
customer reports, not operational confirmations. Setting objection memory off
prevents raw objection/event persistence; enabled lead-signal collection may
still retain a categorical observation. Signals can be disabled independently.

## Database and release steps

Two additive migrations are required:

* `0016_aiactionreceipt`: durable source/action execution results and uniqueness.
* `0017_leadsignal`: unique organization/lead/source/kind/detail observations.

Run the existing migration/deployment workflow before new workers serve traffic.
No existing migration is rewritten. The receipt table must not be rolled back
while new workers are processing messages. Schema additions can remain during a
code rollback.

```bash
python manage.py makemigrations --check --dry-run --settings=config.settings.testing
python manage.py check --settings=config.settings.testing
ruff check .
pytest --cov --cov-fail-under=60
python manage.py evaluate_ai --settings=config.settings.testing --output ai-evaluation.json
```

`evaluate_ai` requires the pinned development/test dependencies and PostgreSQL with
pgvector, as does existing CI. It launches pytest with fixed test settings and a
separate test database. The subprocess uses `config.settings.ai_evaluation`: cache
and channel layers are process-local, the broker/result backend are in-memory,
direct Redis endpoints are disabled, and media lives in its temporary directory.
The ordinary full CI suite still exercises real Redis independently. Optional `--scenarios path.json` accepts bounded declarative
fixtures, not executable Python. Missing reports, zero passing tests, failed
assertions, collection errors, or timeout return failure and block promotion.

The report separates hallucination, qualification, RAG, action, tenant-isolation,
language, intent, silence, and general behaviour failures. Recorded provider
outputs make these backend regressions reproducible; they are **not** a live-model
quality benchmark. External text/embedding providers are mocked and no real
WhatsApp deliveries occur in the scenario tests. Existing transport tests cover
Coexistence's Cloud API routing independently from Hosted linked-device routing.

Promote only the exact successful feature/staging tree. If main has advanced,
prepare a scoped promotion that preserves its newer unrelated features and rerun
CI on that candidate. A green build is not proof of a live account's permissions,
provider credentials, Meta setup, or remote delivery health.

## Capability boundaries

The existing AI contract has no generic live booking executor. Booking requests
therefore remain requests; missing appointment availability remains unknown. A
proposal-only human handoff does not imply a person was notified or assigned.
The existing reminder action can record a callback only with a supported explicit
time. No invented scheduling, delivery, guarantee, or stronger AI model was added.

Natural-language extraction still has bounded deterministic coverage; ambiguous
language continues through the existing model/clarification path. Recordings test
backend decisions and invariants, not every possible phrasing. Organization
configuration remains authoritative for terminology, questions, language and facts.
