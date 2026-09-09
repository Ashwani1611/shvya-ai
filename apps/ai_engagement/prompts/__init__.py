"""Fixed, version-controlled SHVYA AI task prompts.

Organization-specific configuration continues to live in OrgInfo. Knowledge
sources stay in the RAG pipeline. These prompts define only stable SHVYA task
behavior so production does not depend on an external prompt/orchestration
service.
"""

from .bump_up import BUMP_UP_MESSAGE_INSTRUCTIONS
from .engagement import CUSTOMER_ENGAGEMENT_INSTRUCTIONS
from .internal_summary import INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS
from .lead_briefing import LEAD_BRIEFING_INSTRUCTIONS
from .qualification import QUALIFICATION_SUMMARY_INSTRUCTIONS
from .qualification_check import QUALIFICATION_CHECK_INSTRUCTIONS

__all__ = [
    "BUMP_UP_MESSAGE_INSTRUCTIONS",
    "CUSTOMER_ENGAGEMENT_INSTRUCTIONS",
    "INTERNAL_CONVERSATION_SUMMARY_INSTRUCTIONS",
    "LEAD_BRIEFING_INSTRUCTIONS",
    "QUALIFICATION_CHECK_INSTRUCTIONS",
    "QUALIFICATION_SUMMARY_INSTRUCTIONS",
]
