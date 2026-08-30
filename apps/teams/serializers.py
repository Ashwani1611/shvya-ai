from rest_framework import serializers

from apps.teams.models import Team, TeamMembership


class TeamMemberSerializer(serializers.ModelSerializer):
    user_id = serializers.UUIDField(source="user.id", read_only=True)
    name = serializers.CharField(source="user.name", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)

    class Meta:
        model = TeamMembership
        fields = ["user_id", "name", "email", "role", "joined_at"]


class TeamSerializer(serializers.ModelSerializer):
    member_count = serializers.IntegerField(source="memberships.count", read_only=True)
    members = TeamMemberSerializer(source="memberships", many=True, read_only=True)

    class Meta:
        model = Team
        fields = ["id", "name", "description", "is_active", "member_count", "members", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]


class TeamCreateSerializer(serializers.Serializer):
    name = serializers.CharField(required=True, max_length=100)
    description = serializers.CharField(required=False, allow_blank=True, default="")


class TeamUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(required=False, max_length=100)
    description = serializers.CharField(required=False, allow_blank=True)
    is_active = serializers.BooleanField(required=False)


class TeamMemberAddSerializer(serializers.Serializer):
    user_id = serializers.UUIDField(required=True)
    role = serializers.ChoiceField(choices=TeamMembership.Role.choices, required=False, default=TeamMembership.Role.MEMBER)


class TeamMemberRoleUpdateSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=TeamMembership.Role.choices, required=True)