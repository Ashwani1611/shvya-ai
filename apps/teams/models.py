import uuid

from django.db import models

from apps.accounts.models import User
from apps.organizations.models import Organization


class Team(models.Model):
    """
    A group of users within an organization (e.g. "Inbound Sales",
    "Support - Night Shift"). Used to scope lead ownership, follow-up
    routing, and reporting once those features consume it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    members = models.ManyToManyField(User, through="TeamMembership", related_name="teams")

    class Meta:
        app_label = "teams"
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "name"], name="unique_team_name_per_organization"),
        ]
        verbose_name = "Team"
        verbose_name_plural = "Teams"

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    """
    Through-model linking a User to a Team. Kept separate from Team.members
    so we can attach a role without overloading User or Team.
    """

    class Role(models.TextChoices):
        LEAD = "lead", "Team Lead"
        MEMBER = "member", "Member"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="team_memberships")
    role = models.CharField(max_length=20, choices=Role.choices, default=Role.MEMBER)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        app_label = "teams"
        ordering = ["-role", "joined_at"]
        constraints = [
            models.UniqueConstraint(fields=["team", "user"], name="unique_membership_per_team"),
        ]
        verbose_name = "Team Membership"
        verbose_name_plural = "Team Memberships"

    def __str__(self):
        return f"{self.user} in {self.team} ({self.role})"