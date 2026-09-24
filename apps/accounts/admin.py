from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.forms import ReadOnlyPasswordHashField
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html

from .models import SignupVerificationDelivery, SuperadminAccount, User

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
# SUPERADMIN ACCOUNT INFORMATION
# ============================================================


class SuperadminCreationForm(forms.ModelForm):
    password1 = forms.CharField(
        label="Password",
        widget=forms.PasswordInput,
    )
    password2 = forms.CharField(
        label="Password confirmation",
        widget=forms.PasswordInput,
    )

    class Meta:
        model = SuperadminAccount
        fields = (
            "email",
            "name",
            "phone",
            "is_active",
        )

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")

        if password1 != password2:
            raise forms.ValidationError("Passwords do not match.")

        return password2

    def save(self, commit=True):
        user = super().save(commit=False)
        user.organization = None
        user.role = User.Role.SUPERADMIN
        user.is_staff = True
        user.is_superuser = True
        user.set_password(self.cleaned_data["password1"])

        if commit:
            user.save()

        return user


class SuperadminChangeForm(forms.ModelForm):
    password = ReadOnlyPasswordHashField(
        label="Password",
        help_text=(
            "Passwords are not stored in plain text. Use the Reset password "
            "action to set a new password."
        ),
    )

    class Meta:
        model = SuperadminAccount
        fields = (
            "email",
            "password",
            "name",
            "phone",
            "is_active",
        )

    def clean_password(self):
        return self.initial["password"]


@admin.register(SuperadminAccount)
class SuperadminAccountAdmin(BaseUserAdmin):
    """Dedicated SHVYA Admin control panel for platform superadmins."""

    form = SuperadminChangeForm
    add_form = SuperadminCreationForm

    list_display = (
        "email",
        "name",
        "is_active",
        "last_login",
        "reset_password_action",
    )
    list_filter = ("is_active",)
    search_fields = ("email", "name", "phone")
    ordering = ("email",)
    list_per_page = 25
    readonly_fields = (
        "last_login",
        "last_login_at",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            "Account Information",
            {
                "fields": (
                    "email",
                    "password",
                    "name",
                    "phone",
                    "is_active",
                )
            },
        ),
        (
            "Activity",
            {
                "fields": (
                    "last_login",
                    "last_login_at",
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    add_fieldsets = (
        (
            "Create Superadmin",
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "name",
                    "phone",
                    "is_active",
                    "password1",
                    "password2",
                ),
            },
        ),
    )

    def get_queryset(self, request):
        return super().get_queryset(request).filter(
            Q(is_superuser=True) | Q(role=User.Role.SUPERADMIN)
        )

    def save_model(self, request, obj, form, change):
        # A platform superadmin must never inherit a client organization.
        # Normalizing here also repairs legacy role-only superadmin records
        # when they are edited through Account Information.
        obj.organization = None
        obj.role = User.Role.SUPERADMIN
        obj.is_staff = True
        obj.is_superuser = True
        super().save_model(request, obj, form, change)

    @admin.display(description="Password")
    def reset_password_action(self, obj):
        url = reverse(
            "admin:accounts_superadminaccount_password_change",
            args=[obj.pk],
        )
        return format_html('<a class="button" href="{}">Reset password</a>', url)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.pk == request.user.pk:
            return False
        return super().has_delete_permission(request, obj)


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



@admin.register(SignupVerificationDelivery)
class SignupVerificationDeliveryAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "attempts", "next_attempt_at", "delivered_at", "error_type")
    list_filter = ("error_type",)
    readonly_fields = ("user", "email", "verification_endpoint", "attempts", "next_attempt_at", "delivered_at", "error_type")
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
