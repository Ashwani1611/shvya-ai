from rest_framework.permissions import BasePermission


def has_pipeline_permission(
    user,
    pipeline,
    permission,
):
    if not user or not user.is_authenticated:
        return False

    if user.is_superuser:
        return True

    if user.organization_id != pipeline.organization_id:
        return False

    return pipeline.permissions.filter(
        user=user,
        **{
            permission: True,
        },
    ).exists()


class IsOrgMember(BasePermission):

    def has_permission(
        self,
        request,
        view,
    ):
        return bool(
            request.user
            and request.user.is_authenticated
            and request.user.organization_id
        )