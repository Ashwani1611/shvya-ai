from django.contrib import admin

from apps.teams.models import Team, TeamMembership


class TeamMembershipInline(admin.TabularInline):
    model = TeamMembership
    extra = 0
    autocomplete_fields = ["user"]
    fields = ["user", "role", "joined_at"]
    readonly_fields = ["joined_at"]


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "is_active", "member_count", "created_at"]
    list_filter = ["is_active", "organization"]
    search_fields = ["name", "organization__name"]
    inlines = [TeamMembershipInline]

    @admin.display(description="Members")
    def member_count(self, obj):
        return obj.memberships.count()


@admin.register(TeamMembership)
class TeamMembershipAdmin(admin.ModelAdmin):
    list_display = ["team", "user", "role", "joined_at"]
    list_filter = ["role", "team__organization"]
    search_fields = ["team__name", "user__name", "user__email"]
    autocomplete_fields = ["team", "user"]