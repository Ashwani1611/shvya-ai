"""Fixed lead qualification-check instructions."""

QUALIFICATION_CHECK_INSTRUCTIONS = r"""
Analyze the supplied organization configuration and lead conversation to determine
whether the lead has answered the configured qualification questions.

The input contains:
- organization.about
- organization.qualification_requirements
- organization.engagement_instructions
- lead and persisted qualification state
- recent_conversation
- conversation_summary

QUALIFICATION RULES
1. Evaluate every configured qualification requirement independently.
2. A qualification requirement counts as answered when the lead has clearly
   supplied the requested information in the conversation or verified persisted
   qualification state.
3. A question does NOT need to have been explicitly asked first. If the lead
   volunteered information that clearly answers a configured requirement, count
   that requirement as answered.
4. Qualification is based on whether the requested information was answered, not
   on whether the answer is positive, desirable, or commercially attractive.
5. Do not invent answers or infer missing information from enthusiasm,
   politeness, profession, phone number, or unrelated CRM data.
6. Interpret short answers such as A/B/C/D, numbers, Yes/No, "sure", or similar
   responses only in the context of the qualification question that was actually
   active/asked at that point in the conversation or in verified backend state.
7. Newer explicit lead statements override older summaries or extracted values
   when they conflict.
8. answered and not_applicable may satisfy completion. unknown and unclear do
   not satisfy completion.

ALL-QUESTIONS OVERRIDE
- Inspect BOTH organization.qualification_requirements and
  organization.engagement_instructions.
- If either explicitly says that all qualification questions/requirements must be
  answered before the lead is qualified, set qualified=true ONLY when every
  required applicable qualification question has been answered.
- Examples of this intent include wording such as "qualify only when all
  questions have been answered", "all questions must be answered", or an
  equivalent explicit rule.

DEFAULT MAJORITY RULE
- When there is NO explicit all-questions requirement, consider the lead
  qualified when the lead has answered the majority of the configured
  qualification questions.
- Majority means strictly more than half of the required applicable questions.
- Do not apply a stricter completion rule unless the organization configuration
  explicitly requires it.

OUTPUT FIELDS
- all_questions_answered=true only when every required applicable qualification
  question is answered.
- qualified follows the all-questions override when configured; otherwise it
  follows the majority rule above.
- reason must be short and factual. If complete, use a concise reason such as
  "all information received". If incomplete, state the important missing
  qualification information without exposing hidden reasoning.
- Do not output chain-of-thought, markdown, comments, or additional keys.

Return ONLY one valid JSON object with exactly this schema:
{
  "qualified": boolean,
  "reason": string,
  "all_questions_answered": boolean
}
""".strip()
