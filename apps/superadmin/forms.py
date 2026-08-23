from django import forms

from apps.accounts.models import User
from apps.crm.models import Pipeline
from apps.organizations.models import (
    Organization,
    OrganizationPayment,
)


# ============================================================
# ORGANIZATION CREATE FORM
# ============================================================


class OrganizationCreateForm(forms.ModelForm):
    """
    Superadmin form for creating a new organization.

    The organization is created directly from the
    Superadmin Organization Console.

    Only organization-level information is collected here.
    Users can be added later from the organization detail page.
    """

    class Meta:
        model = Organization

        fields = (
            "name",
            "package",
            "payment_mode",
            "number_of_seats",
            "credits_total",
            "credits_alert_enabled",
            "total_sale_amount",
            "assigned_poc",
            "tags",
            "operational_notes",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter organization name",
                    "autofocus": True,
                }
            ),
            "package": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "payment_mode": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "number_of_seats": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "1",
                }
            ),
            "credits_total": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "0",
                }
            ),
            "credits_alert_enabled": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300",
                }
            ),
            "total_sale_amount": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "0",
                    "step": "0.01",
                }
            ),
            "assigned_poc": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "tags": forms.SelectMultiple(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "size": "5",
                }
            ),
            "operational_notes": forms.Textarea(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "rows": "4",
                    "placeholder": (
                        "Add internal operational notes..."
                    ),
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # ---------------------------------------------------------
        # Assigned POC
        # ---------------------------------------------------------

        self.fields["assigned_poc"].queryset = (
            User.objects
            .filter(
                organization__isnull=True,
            )
            .order_by(
                "email",
            )
        )

        # ---------------------------------------------------------
        # Optional fields
        # ---------------------------------------------------------

        self.fields["credits_total"].required = False
        self.fields["credits_alert_enabled"].required = False
        self.fields["total_sale_amount"].required = False
        self.fields["assigned_poc"].required = False
        self.fields["tags"].required = False
        self.fields["operational_notes"].required = False

        # ---------------------------------------------------------
        # Labels
        # ---------------------------------------------------------

        self.fields["name"].label = "Organization Name"
        self.fields["package"].label = "Package"
        self.fields["payment_mode"].label = "Payment Mode"
        self.fields["number_of_seats"].label = "Number of Seats"
        self.fields["credits_total"].label = "Total Credits"
        self.fields["credits_alert_enabled"].label = "Enable Credit Alert"
        self.fields["total_sale_amount"].label = "Total Sale Amount"
        self.fields["assigned_poc"].label = "Assigned POC"
        self.fields["tags"].label = "Organization Tags"
        self.fields["operational_notes"].label = "Operational Notes"

    def clean(self):
        cleaned_data = super().clean()

        name = cleaned_data.get("name")

        if name:
            name = name.strip()

            if not name:
                self.add_error(
                    "name",
                    "Organization name is required.",
                )

            cleaned_data["name"] = name

            # -----------------------------------------------------
            # Prevent duplicate organization names
            # -----------------------------------------------------

            if Organization.objects.filter(
                name__iexact=name,
            ).exists():
                self.add_error(
                    "name",
                    "An organization with this name already exists.",
                )

        return cleaned_data


# ============================================================
# ORGANIZATION UPDATE FORM
# ============================================================


class OrganizationUpdateForm(forms.ModelForm):
    class Meta:
        model = Organization

        fields = (
            "name",
            "assigned_poc",
            "credits_total",
            "credits_used",
            "credits_alert_enabled",
            "renewal_payment_at",
            "day_of_sale",
            "onboarding_completion_date",
            "number_of_seats",
            "package",
            "tags",
            "operational_notes",
            "total_sale_amount",
            "payment_mode",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "assigned_poc": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "credits_total": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "0",
                }
            ),
            "credits_used": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "0",
                }
            ),
            "credits_alert_enabled": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300",
                }
            ),
            "renewal_payment_at": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "day_of_sale": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "onboarding_completion_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "number_of_seats": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "1",
                }
            ),
            "package": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "tags": forms.SelectMultiple(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "size": "5",
                }
            ),
            "operational_notes": forms.Textarea(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "rows": "4",
                }
            ),
            "total_sale_amount": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "min": "0",
                    "step": "0.01",
                }
            ),
            "payment_mode": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        organization = kwargs.pop(
            "organization",
            None,
        )

        super().__init__(
            *args,
            **kwargs,
        )

        # ---------------------------------------------------------
        # Assigned POC
        # ---------------------------------------------------------

        if "assigned_poc" in self.fields:

            if organization is not None:
                self.fields["assigned_poc"].queryset = (
                    User.objects.filter(
                        organization__isnull=True
                    )
                    | User.objects.filter(
                        organization=organization
                    )
                ).distinct().order_by(
                    "email",
                )

            else:
                self.fields["assigned_poc"].queryset = (
                    User.objects
                    .filter(
                        organization__isnull=True
                    )
                    .order_by(
                        "email",
                    )
                )

        self.fields["assigned_poc"].required = False

        # ---------------------------------------------------------
        # Labels
        # ---------------------------------------------------------

        self.fields["credits_total"].label = "Total Credits"
        self.fields["credits_used"].label = "Credits Used"
        self.fields["credits_alert_enabled"].label = "Enable Credit Alert"
        self.fields["renewal_payment_at"].label = "Renewal Payment At"
        self.fields["day_of_sale"].label = "Day of Sale"
        self.fields[
            "onboarding_completion_date"
        ].label = "Onboarding Completion Date"
        self.fields["number_of_seats"].label = "Number of Seats"
        self.fields["total_sale_amount"].label = "Total Sale Amount"
        self.fields["payment_mode"].label = "Payment Mode"

    def clean(self):
        cleaned_data = super().clean()

        credits_total = cleaned_data.get(
            "credits_total",
        )

        credits_used = cleaned_data.get(
            "credits_used",
        )

        if (
            credits_total is not None
            and credits_used is not None
            and credits_used > credits_total
        ):
            self.add_error(
                "credits_used",
                "Credits used cannot be greater than total credits.",
            )

        return cleaned_data


# ============================================================
# ORGANIZATION PAYMENT FORM
# ============================================================


class OrganizationPaymentForm(forms.ModelForm):
    """
    Superadmin form for creating and editing an individual
    organization payment.

    Used by:

        Step 3E — Add Payment
        Step 3F — Edit Payment
    """

    class Meta:
        model = OrganizationPayment

        fields = (
            "amount",
            "payment_date",
            "payment_method",
            "reference_number",
            "notes",
        )

        widgets = {
            "amount": forms.NumberInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter payment amount",
                    "min": "0",
                    "step": "0.01",
                }
            ),
            "payment_date": forms.DateInput(
                attrs={
                    "type": "date",
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "payment_method": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "reference_number": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": (
                        "Transaction / reference number"
                    ),
                }
            ),
            "notes": forms.Textarea(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "rows": "4",
                    "placeholder": (
                        "Add payment notes..."
                    ),
                }
            ),
        }

    def __init__(
        self,
        *args,
        organization=None,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.organization = organization

        # ---------------------------------------------------------
        # Labels
        # ---------------------------------------------------------

        self.fields["amount"].label = "Amount"
        self.fields["payment_date"].label = "Payment Date"
        self.fields["payment_method"].label = "Payment Method"
        self.fields["reference_number"].label = "Reference Number"
        self.fields["notes"].label = "Notes"

        # ---------------------------------------------------------
        # Required / optional fields
        # ---------------------------------------------------------

        self.fields["amount"].required = True
        self.fields["payment_date"].required = True
        self.fields["payment_method"].required = True
        self.fields["reference_number"].required = False
        self.fields["notes"].required = False

    def clean_amount(self):
        amount = self.cleaned_data.get(
            "amount",
        )

        if amount is None:
            return amount

        if amount <= 0:
            raise forms.ValidationError(
                "Payment amount must be greater than 0."
            )

        return amount

    def save(self, commit=True):
        payment = super().save(
            commit=False,
        )

        if self.organization is not None:
            payment.organization = self.organization

        if commit:
            payment.save()

        return payment


# ============================================================
# ORGANIZATION USER FORM
# ============================================================


class OrganizationUserForm(forms.ModelForm):
    """
    Superadmin form for creating an organization user.

    IMPORTANT:
    The organization is NOT a form field.

    The organization must be supplied explicitly by the view:

        form.save(organization=organization)

    Superadmin users cannot be created through this form.
    """

    password = forms.CharField(
        label="Password",
        widget=forms.PasswordInput(
            attrs={
                "class": (
                    "w-full border rounded-md "
                    "px-3 py-2 text-sm"
                ),
                "placeholder": "Enter password",
                "autocomplete": "new-password",
            }
        ),
        required=True,
        min_length=8,
    )

    password_confirm = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(
            attrs={
                "class": (
                    "w-full border rounded-md "
                    "px-3 py-2 text-sm"
                ),
                "placeholder": "Confirm password",
                "autocomplete": "new-password",
            }
        ),
        required=True,
        min_length=8,
    )

    class Meta:
        model = User

        fields = (
            "name",
            "email",
            "phone",
            "role",
            "is_active",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter full name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter email address",
                }
            ),
            "phone": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter phone number",
                }
            ),
            "role": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300",
                }
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(
            *args,
            **kwargs,
        )

        # ---------------------------------------------------------
        # Organization roles only
        # ---------------------------------------------------------

        self.fields["role"].choices = [
            choice
            for choice in User.Role.choices
            if choice[0] != User.Role.SUPERADMIN
        ]

        # ---------------------------------------------------------
        # Labels
        # ---------------------------------------------------------

        self.fields["name"].label = "Full Name"
        self.fields["email"].label = "Email"
        self.fields["phone"].label = "Phone"
        self.fields["role"].label = "Role"
        self.fields["is_active"].label = "Active"

    def clean_email(self):
        email = self.cleaned_data.get("email")

        if not email:
            return email

        email = User.objects.normalize_email(email)

        if User.objects.filter(
            email__iexact=email,
        ).exists():
            raise forms.ValidationError(
                "A user with this email address already exists."
            )

        return email

    def clean_role(self):
        role = self.cleaned_data.get("role")

        if role == User.Role.SUPERADMIN:
            raise forms.ValidationError(
                "Superadmin users cannot be created from an "
                "organization account."
            )

        return role

    def clean(self):
        cleaned_data = super().clean()

        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get("password_confirm")

        if (
            password
            and password_confirm
            and password != password_confirm
        ):
            self.add_error(
                "password_confirm",
                "Passwords do not match.",
            )

        return cleaned_data

    def save(self, commit=True, organization=None):
        """
        Save the organization user.

        The organization is deliberately passed separately
        from cleaned_data so it cannot accidentally be supplied
        twice to UserManager.create_user().

        Password is hashed using Django's set_password().
        """

        if organization is None:
            raise ValueError(
                "Organization is required when creating "
                "an organization user."
            )

        user = super().save(
            commit=False,
        )

        # ---------------------------------------------------------
        # Explicit organization assignment
        # ---------------------------------------------------------

        user.organization = organization

        # ---------------------------------------------------------
        # Explicitly prevent accidental Superadmin creation
        # ---------------------------------------------------------

        if user.role == User.Role.SUPERADMIN:
            raise ValueError(
                "Superadmin users cannot be created through "
                "OrganizationUserForm."
            )

        # ---------------------------------------------------------
        # Secure password hashing
        # ---------------------------------------------------------

        user.set_password(
            self.cleaned_data["password"],
        )

        if commit:
            user.save()

        return user


# ============================================================
# ORGANIZATION USER UPDATE FORM
# ============================================================


class OrganizationUserUpdateForm(forms.ModelForm):
    """
    Superadmin form for editing an existing organization user.

    Password is intentionally excluded.

    Password reset is handled separately.

    The organization is never exposed as a form field.
    """

    class Meta:
        model = User

        fields = (
            "name",
            "email",
            "phone",
            "role",
            "is_active",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter full name",
                }
            ),
            "email": forms.EmailInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter email address",
                }
            ),
            "phone": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter phone number",
                }
            ),
            "role": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "rounded border-gray-300",
                }
            ),
        }

    def __init__(
        self,
        *args,
        organization=None,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.organization = organization

        # ---------------------------------------------------------
        # Organization roles only
        # ---------------------------------------------------------

        self.fields["role"].choices = [
            choice
            for choice in User.Role.choices
            if choice[0] != User.Role.SUPERADMIN
        ]

        # ---------------------------------------------------------
        # Labels
        # ---------------------------------------------------------

        self.fields["name"].label = "Full Name"
        self.fields["email"].label = "Email"
        self.fields["phone"].label = "Phone"
        self.fields["role"].label = "Role"
        self.fields["is_active"].label = "Active"

    def clean_email(self):
        email = self.cleaned_data.get("email")

        if not email:
            return email

        email = User.objects.normalize_email(email)

        # ---------------------------------------------------------
        # Allow current user's existing email.
        #
        # Reject only when another user owns the email.
        # ---------------------------------------------------------

        queryset = (
            User.objects
            .filter(
                email__iexact=email,
            )
            .exclude(
                pk=self.instance.pk,
            )
        )

        if queryset.exists():
            raise forms.ValidationError(
                "A user with this email address already exists."
            )

        return email

    def clean_role(self):
        role = self.cleaned_data.get("role")

        if role == User.Role.SUPERADMIN:
            raise forms.ValidationError(
                "Superadmin users cannot be assigned "
                "to an organization user."
            )

        return role

    def clean(self):
        cleaned_data = super().clean()

        # ---------------------------------------------------------
        # Organization isolation
        # ---------------------------------------------------------

        if (
            self.instance.pk
            and self.organization is not None
            and self.instance.organization_id
            != self.organization.id
        ):
            raise forms.ValidationError(
                "This user does not belong to the selected "
                "organization."
            )

        return cleaned_data


# ============================================================
# ORGANIZATION USER PASSWORD RESET FORM
# ============================================================


class OrganizationUserPasswordResetForm(forms.Form):
    """
    Superadmin form for resetting an existing organization
    user's password.

    Password reset is intentionally handled separately from
    the normal organization user edit form.

    The password is never displayed or stored as plain text.

    The corresponding view should use Django's
    set_password() mechanism to securely hash the new password.
    """

    password = forms.CharField(
        label="New Password",
        min_length=8,
        required=True,
        widget=forms.PasswordInput(
            attrs={
                "class": (
                    "w-full border rounded-md "
                    "px-3 py-2 text-sm"
                ),
                "placeholder": "Enter new password",
                "autocomplete": "new-password",
            }
        ),
    )

    password_confirm = forms.CharField(
        label="Confirm New Password",
        min_length=8,
        required=True,
        widget=forms.PasswordInput(
            attrs={
                "class": (
                    "w-full border rounded-md "
                    "px-3 py-2 text-sm"
                ),
                "placeholder": "Confirm new password",
                "autocomplete": "new-password",
            }
        ),
    )

    def clean(self):
        cleaned_data = super().clean()

        password = cleaned_data.get("password")
        password_confirm = cleaned_data.get(
            "password_confirm",
        )

        if (
            password
            and password_confirm
            and password != password_confirm
        ):
            self.add_error(
                "password_confirm",
                "Passwords do not match.",
            )

        return cleaned_data


# ============================================================
# PIPELINE CREATE FORM
# ============================================================


class PipelineCreateForm(forms.ModelForm):
    """
    Superadmin form for creating/editing a pipeline.

    The organization is supplied by the Superadmin view and
    is never exposed as a selectable form field.

    The pipeline owner must be an actual user belonging to
    the selected organization.
    """

    class Meta:
        model = Pipeline

        fields = (
            "name",
            "country_code",
            "phone_number",
            "owner",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Enter pipeline name",
                }
            ),
            "country_code": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Country code",
                }
            ),
            "phone_number": forms.TextInput(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                    "placeholder": "Phone number",
                }
            ),
            "owner": forms.Select(
                attrs={
                    "class": (
                        "w-full border rounded-md "
                        "px-3 py-2 text-sm"
                    ),
                }
            ),
        }

    def __init__(
        self,
        *args,
        organization=None,
        **kwargs,
    ):
        super().__init__(
            *args,
            **kwargs,
        )

        self.organization = organization

        # ---------------------------------------------------------
        # Owner
        # ---------------------------------------------------------

        if organization is not None:
            self.fields["owner"].queryset = (
                User.objects
                .filter(
                    organization=organization,
                )
                .order_by(
                    "email",
                )
            )
        else:
            self.fields["owner"].queryset = User.objects.none()

        # ---------------------------------------------------------
        # Labels
        # ---------------------------------------------------------

        self.fields["name"].label = "Pipeline Name"
        self.fields["country_code"].label = "Country Code"
        self.fields["phone_number"].label = "Phone Number"
        self.fields["owner"].label = "Owner"

    def clean_name(self):
        name = self.cleaned_data.get(
            "name",
        )

        if not name:
            return name

        name = name.strip()

        if not name:
            raise forms.ValidationError(
                "Pipeline name is required."
            )

        if self.organization is not None:
            queryset = Pipeline.objects.filter(
                organization=self.organization,
                name__iexact=name,
            )

            if self.instance.pk:
                queryset = queryset.exclude(
                    pk=self.instance.pk,
                )

            if queryset.exists():
                raise forms.ValidationError(
                    "A pipeline with this name already exists "
                    "in this organization."
                )

        return name

    def clean_owner(self):
        owner = self.cleaned_data.get(
            "owner",
        )

        if owner is None:
            return owner

        if self.organization is None:
            raise forms.ValidationError(
                "Organization is required."
            )

        if owner.organization_id != self.organization.id:
            raise forms.ValidationError(
                "Pipeline owner must belong to this organization."
            )

        if owner.role == User.Role.SUPERADMIN:
            raise forms.ValidationError(
                "Superadmin cannot be assigned as a pipeline owner."
            )

        return owner

    def save(self, commit=True):
        pipeline = super().save(
            commit=False,
        )

        if self.organization is None:
            raise ValueError(
                "Organization is required when creating a pipeline."
            )

        pipeline.organization = self.organization

        if commit:
            pipeline.save()

        return pipeline