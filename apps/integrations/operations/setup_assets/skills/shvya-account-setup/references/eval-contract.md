# Skill evaluation contract

Behavioral eval files are rubrics, not claims that a model/provider test ran.

Every top-level skill should include `evals/evals.json` with scenarios covering at least:

1. **happy path** — correct bounded use of the skill;
2. **false-positive trap** — a plausible symptom that must not be misdiagnosed;
3. **missing/contradictory evidence** — the skill must preserve UNKNOWN/conflict rather than invent;
4. **authority/safety boundary** — unsupported, cross-tenant, secret-bearing or unauthorized action is not bypassed;
5. **verification failure** where meaningful — a successful save/queue does not equal the requested business outcome.

Expected outputs should describe observable behavior, evidence used, tool/order expectations, prohibited shortcuts and final classification. Do not require exact prose.

Use `result: null` until an actual evaluator records an outcome. Keep eval data free of real secrets and unnecessary personal data.
