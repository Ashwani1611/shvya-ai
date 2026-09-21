from django.contrib import admin

from .models import (
    CalendarBlock,
    CalendarBooking,
    CalendarPage,
    CalendarPageVersion,
    CalendarReminderDelivery,
    CalendarReminderSequence,
    CalendarReminderStep,
    CalendarSubmission,
    CalendarSubmissionAttachment,
    GoogleCalendarConnection,
)


@admin.register(CalendarPage)
class CalendarPageAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "status", "page_type", "updated_at")
    list_filter = ("status", "page_type", "organization")
    search_fields = ("name", "slug", "organization__name")


@admin.register(CalendarBooking)
class CalendarBookingAdmin(admin.ModelAdmin):
    list_display = ("lead", "page", "start_at", "status", "calendar_sync_status")
    list_filter = ("status", "calendar_sync_status", "organization")
    search_fields = ("lead__name", "lead__phone", "page__name")


admin.site.register(CalendarPageVersion)
admin.site.register(CalendarSubmission)
admin.site.register(CalendarSubmissionAttachment)
admin.site.register(CalendarBlock)
admin.site.register(GoogleCalendarConnection)
admin.site.register(CalendarReminderSequence)
admin.site.register(CalendarReminderStep)
admin.site.register(CalendarReminderDelivery)
