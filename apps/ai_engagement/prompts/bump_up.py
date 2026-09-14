"""Fixed scheduled re-engagement message instructions."""

BUMP_UP_MESSAGE_INSTRUCTIONS = r"""This is a scheduled bump-up. The lead has not replied for at least one hour.
Write one brief, natural reminder based on the previous conversation. Do not
repeat the last message verbatim, invent urgency, or mention automation. The
backend has already decided that this bump-up is eligible, so return
should_engage=true and silence_rule=null. Do not turn an organization instruction
into a model-authored no-reply decision. Do not request CRM actions or a file
attachment. If a business fact is not verified in the supplied context, do not
invent it.
"""
