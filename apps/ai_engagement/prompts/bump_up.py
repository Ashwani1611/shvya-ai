"""Fixed scheduled re-engagement message instructions."""

BUMP_UP_MESSAGE_INSTRUCTIONS = r"""This is a scheduled bump-up. The lead has not replied for at least one hour.
Write one brief, natural reminder based on the previous conversation. Do not
repeat the last message verbatim, invent urgency, or mention automation. Set
should_engage=true unless the organization explicitly requires silence in its
Qualification Requirements or Engagement Instructions. Cite that exact rule in
silence_rule and use ORG_INSTRUCTION when returning should_engage=false. Do not request CRM actions or a file attachment.
"""
