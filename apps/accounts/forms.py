from django import forms

from .models import User


class OrganizationUserForm(forms.ModelForm):
    """
    Form used by Superadmin to create and edit users
    belonging to a specific organization.
    """

    password = forms.CharField(
        required=False,
        widget=forms.PasswordInput(
            attrs={
                "class": "w-full border rounded-md px-3 py-2 text-sm",
                "autocomplete": "new-password",
            }
        ),
    )

    class Meta:
        model = User

        fields = (
            "name",
            "email",
            "phone",
            "role",
            "is_active",
            "password",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full border rounded-md px-3 py-2 text-sm",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": "w-full border rounded-md px-3 py-2 text-sm",
                    "autocomplete": "email",
                }
            ),
            "phone": forms.TextInput(
                attrs={
                    "class": "w-full border rounded-md px-3 py-2 text-sm",
                }
            ),
            "role": forms.Select(
                attrs={
                    "class": "w-full border rounded-md px-3 py-2 text-sm",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300",
                }
            ),
        }

    def __init__(self, *args, organization=None, **kwargs):
        self.organization = organization

        super().__init__(*args, **kwargs)

        # -----------------------------------------------------
        # Role
        # -----------------------------------------------------
        #
        # Organization users must never be Superadmins.
        #
        self.fields["role"].choices = [
            choice
            for choice in User.Role.choices
            if choice[0] != User.Role.SUPERADMIN
        ]

        # -----------------------------------------------------
        # Password
        # -----------------------------------------------------

        if self.instance and self.instance.pk:
            self.fields["password"].required = False
            self.fields["password"].help_text = (
                "Leave blank to keep the current password."
            )
        else:
            self.fields["password"].required = True
            self.fields["password"].help_text = (
                "Password is required when creating a new user."
            )

        # -----------------------------------------------------
        # Labels
        # -----------------------------------------------------

        self.fields["name"].label = "Full Name"
        self.fields["email"].label = "Email Address"
        self.fields["phone"].label = "Phone"
        self.fields["role"].label = "Role"
        self.fields["is_active"].label = "Active"
        self.fields["password"].label = "Password"

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()

        queryset = User.objects.filter(
            email__iexact=email
        )

        if self.instance and self.instance.pk:
            queryset = queryset.exclude(
                pk=self.instance.pk
            )

        if queryset.exists():
            raise forms.ValidationError(
                "A user with this email address already exists."
            )

        return email

    def clean_role(self):
        role = self.cleaned_data["role"]

        if role == User.Role.SUPERADMIN:
            raise forms.ValidationError(
                "Superadmin users cannot be assigned to an organization."
            )

        return role

    def clean(self):
        cleaned_data = super().clean()

        if self.organization is None:
            raise forms.ValidationError(
                "An organization is required for this user."
            )

        return cleaned_data

    def save(self, commit=True):
        user = super().save(commit=False)

        # Always force the user into the organization being
        # managed by Superadmin.
        user.organization = self.organization

        password = self.cleaned_data.get("password")

        if password:
            user.set_password(password)

        if commit:
            user.save()

        return user