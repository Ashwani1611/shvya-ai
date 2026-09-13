"""Fixed lead qualification-check instructions."""

QUALIFICATION_CHECK_INSTRUCTIONS = r"""
Analyze the supplied organization configuration, persisted qualification state,
and actual lead conversation to determine whether the configured qualification
requirements are complete.

The input contains:
- organization.about
- organization.qualification_requirements
- organization.engagement_instructions
- lead and persisted qualification state
- recent_conversation
- conversation_summary

QUALIFICATION RULES
1. Treat persisted backend qualification state as authoritative when it contains
   a state for a configured requirement. Do not make a completed requirement
   unanswered merely because later conversation text is unrelated.
2. Evaluate every required applicable qualification requirement independently.
3. A requirement counts as answered when verified persisted state says answered,
   or when clear inbound conversation evidence supplies the requested information.
4. A question does NOT need to have been explicitly asked first. If the lead
   volunteered information that clearly answers a configured requirement, count
   that requirement as answered.
5. Qualification is based on whether required information was supplied, not on
   enthusiasm, commercial attractiveness, demo interest, or positive sentiment.
6. Interpret A/B/C/D, numbers, Yes/No, and other short replies only against the
   qualification question that was active/asked at that point according to
   verified backend/conversation context. Never search the full questionnaire for
   a convenient meaning for an ambiguous short answer.
7. Newer explicit lead corrections override older extracted values when they
   clearly refer to the same requirement.
8. answered and not_applicable satisfy completion. unknown, asked, and unclear do
   not satisfy completion. skipped satisfies completion only when the configured
   organization flow allows that requirement to be skipped.
9. Never invent missing answers or infer them from profession, phone number,
   politeness, booking/demo interest, or unrelated CRM data.

COMPLETION POLICY
- Default behavior is strict required-item completion: qualified=true only when
  every required applicable configured qualification requirement is complete.
- Inspect BOTH organization.qualification_requirements and
  organization.engagement_instructions for an explicit organization-authored
  alternative completion policy.
- Apply a majority/threshold rule ONLY when the organization explicitly configures
  that policy. Never apply a generic majority rule by default.
- If the organization explicitly says all questions/requirements must be answered,
  qualified=true only when all required applicable items are complete.
- If persisted backend qualification state says the flow is completed and its
  configured requirement states support that completion, do not reopen it.

OUTPUT FIELDS
- all_questions_answered=true only when every required applicable qualification
  requirement is complete.
- qualified follows the configured completion policy, with strict all-required
  completion as the default.
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