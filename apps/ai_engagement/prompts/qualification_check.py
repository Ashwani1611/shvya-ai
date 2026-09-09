"""Fixed lead qualification-check instructions."""

QUALIFICATION_CHECK_INSTRUCTIONS = r"""
Analyze only the supplied organization qualification requirements, structured
requirement state, supported CRM data, and actual conversation evidence.

Rules:
- Evaluate every configured requirement independently.
- A requirement counts as answered only when the actual conversation, supported
  CRM data, or application-bound LAST_ASKED_REQUIREMENT_ID clearly supplies the
  answer.
- Newer explicit customer statements override older summaries or extracted
  values when they conflict.
- Do not bind isolated short replies such as "yes", "no", "maybe", "soon",
  "fine", or "okay" to a requirement unless the supplied context makes that
  relationship unambiguous.
- Do not judge qualification from enthusiasm, politeness, profession, phone
  number, or other unsupported inference.
- Do not invent missing answers.
- answered and not_applicable satisfy completion; unknown and unclear do not.
- If the organization explicitly requires all questions, qualified may be true
  only when all required items are complete and any configured pass/fail rules
  are satisfied.
- If majority-based qualification is explicitly configured, follow that rule.
- Application-level deterministic qualification state overrides generic prompt
  assumptions.
- all_questions_answered is true only when no required item remains unknown or
  unclear.
- reason must be a short factual description of what is complete or missing.
- Do not output chain-of-thought.

Return ONLY valid JSON:
{
  "qualified": boolean,
  "reason": string,
  "all_questions_answered": boolean
}
""".strip()
