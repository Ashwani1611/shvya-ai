"""Fixed lead qualification-check instructions."""

QUALIFICATION_CHECK_INSTRUCTIONS = r"""
Analyze the supplied organization qualification requirements and conversation
evidence to determine whether the required qualification information is known.

Rules:
- A requirement counts as answered when the conversation or supported CRM data
  clearly contains the information, even when the bot did not explicitly ask
  that exact question.
- Do not judge qualification from the wording of an answer unless the
  organization's requirement itself defines a pass/fail condition.
- If the organization explicitly says every question/requirement must be
  answered, qualified may be true only when every requirement is answered.
- Otherwise, when the organization explicitly allows majority-based
  qualification, follow that configured rule. Application-level qualification
  rules supplied at runtime override this default.
- Never invent missing answers.
- all_questions_answered is true only when no required item remains unknown.
- reason must be a short factual description of what is complete or missing.
- Do not output chain-of-thought.

Return ONLY valid JSON:
{
  "qualified": boolean,
  "reason": string,
  "all_questions_answered": boolean
}
""".strip()
