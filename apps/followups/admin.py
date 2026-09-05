from django.contrib import admin

from apps.followups.models import (
    AutoFollowupSettings,
    FollowupExecution,
    FollowupSenderState,
    FollowupSequence,
    FollowupStep,
    LeadSequenceState,
)


admin.site.register(AutoFollowupSettings)
admin.site.register(FollowupSequence)
admin.site.register(FollowupStep)
admin.site.register(LeadSequenceState)
admin.site.register(FollowupExecution)
admin.site.register(FollowupSenderState)
