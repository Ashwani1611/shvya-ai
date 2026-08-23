from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField

from .models import User


# ============================================================
# USER CREATION FORM
# ============================================================

class UserCreationForm(
    forms.ModelForm
):
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
    )

    password2 = forms.CharField(
        label="Password confirmation",
        widget=forms.PasswordInput,
    )

    class Meta:
        model = User

        fields = (
            "email",
            "organization",
            "name",
            "phone",
            "role",
        )

    def clean_password2(self):
        password1 = self.cleaned_data.get(
            "password1"
        )

        password2 = self.cleaned_data.get(
            "password2"
        )

        if password1 != password2:
            raise forms.ValidationError(
                "Passwords do not match."
            )

        return password2

    def save(
        self,
        commit=True,
    ):
        user = super().save(
            commit=False
        )

        user.set_password(
            self.cleaned_data["password1"]
        )

        if commit:
            user.save()

        return user


# ============================================================
# USER CHANGE FORM
# ============================================================

class UserChangeForm(
    forms.ModelForm
):
    password = ReadOnlyPasswordHashField(
        label="Password",
    )

    class Meta:
        model = User

        fields = (
            "email",
            "password",
            "organization",
            "name",
            "phone",
            "role",
            "is_active",
            "is_staff",
            "is_superuser",
        )

    def clean_password(self):
        return self.initial["password"]


# ============================================================
# USER ADMIN
# ============================================================

@admin.register(User)
class UserAdmin(
    BaseUserAdmin
):
    """
    SHVYA Admin user management.

    Provides operational filtering and search across
    organization, role, account status, and access level.
    """

    form = UserChangeForm

    add_form = UserCreationForm

    # ---------------------------------------------------------
    # List display
    # ---------------------------------------------------------

    list_display = (
        "email",
        "name",
        "organization",
        "role",
        "is_active",
        "is_staff",
    )

    # ---------------------------------------------------------
    # Operational filters
    # ---------------------------------------------------------

    list_filter = (
        "organization",
        "role",
        "is_active",
        "is_staff",
        "is_superuser",
    )

    # ---------------------------------------------------------
    # Global user search
    # ---------------------------------------------------------

    search_fields = (
        "email",
        "name",
        "phone",
        "organization__name",
    )

    # ---------------------------------------------------------
    # Ordering
    # ---------------------------------------------------------

    ordering = (
        "email",
    )

    # ---------------------------------------------------------
    # Pagination
    # ---------------------------------------------------------

    list_per_page = 25

    # ---------------------------------------------------------
    # User edit fieldsets
    # ---------------------------------------------------------

    fieldsets = (
        (
            None,
            {
                "fields": (
                    "email",
                    "password",
                )
            },
        ),
        (
            "Organization",
            {
                "fields": (
                    "organization",
                    "role",
                )
            },
        ),
        (
            "Personal information",
            {
                "fields": (
                    "name",
                    "phone",
                )
            },
        ),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                )
            },
        ),
        (
            "Important dates",
            {
                "fields": (
                    "last_login",
                    "last_login_at",
                )
            },
        ),
    )

    # ---------------------------------------------------------
    # User creation
    # ---------------------------------------------------------

    add_fieldsets = (
        (
            None,
            {
                "classes": (
                    "wide",
                ),
                "fields": (
                    "email",
                    "organization",
                    "name",
                    "phone",
                    "role",
                    "password1",
                    "password2",
                ),
            },
        ),
    )