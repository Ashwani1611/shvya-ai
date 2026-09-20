from apps.accounts.models import User


def is_sales_admin(user):
    return bool(
        user
        and getattr(user, "organization_id", None)
        and getattr(user, "role", None) == User.Role.ADMIN
    )
