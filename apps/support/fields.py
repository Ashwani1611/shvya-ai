from datetime import date
from decimal import Decimal

from django import forms
from django.core.exceptions import ValidationError
from .models import CustomField


def field_for(definition, *, required=None):
    kwargs = {"label": definition.name, "help_text": definition.help_text,
              "required": definition.required if required is None else required}
    kind = definition.kind
    if kind == "checkbox":
        return forms.BooleanField(**kwargs)
    if kind == "number":
        return forms.DecimalField(max_digits=16, decimal_places=4, **kwargs)
    if kind == "date":
        return forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), **kwargs)
    if kind == "select":
        return forms.ChoiceField(choices=[("", "Select an option")] + [(s, s) for s in definition.choices], **kwargs)
    return forms.CharField(max_length=5000 if kind == "long" else 500,
                           widget=forms.Textarea(attrs={"rows": 3}) if kind == "long" else forms.TextInput, **kwargs)


def validate_custom(data, *, staff=False, existing=None, closing=False, partial=False):
    data = data or {}
    if not isinstance(data, dict):
        raise ValidationError("Custom fields must be an object.")
    definitions = list(CustomField.objects.filter(active=True))
    visible = {d.key: d for d in definitions if staff or d.customer_visible}
    if set(data) - set(visible):
        raise ValidationError("An unknown or private custom field was supplied.")
    values = dict(existing or {})
    errors = {}
    for key, definition in visible.items():
        required = definition.required or (closing and definition.required_on_close)
        if partial and key not in data and not closing:
            continue
        value = data.get(key, values.get(key))
        try:
            clean = field_for(definition, required=required).clean(value)
            values[key] = str(clean) if isinstance(clean, (date, Decimal)) else clean
        except ValidationError as exc:
            errors[definition.name] = exc.messages
    # Internal close-required fields must also be complete when a customer closes.
    if closing and not staff:
        for d in definitions:
            if not d.customer_visible and d.required_on_close and not values.get(d.key):
                errors["Status"] = ["Support staff must complete resolution details before this ticket can close."]
    if errors:
        raise ValidationError(errors)
    return values


def public_custom(values):
    return [(d.name, values.get(d.key)) for d in CustomField.objects.filter(active=True, customer_visible=True)
            if values.get(d.key) not in (None, "")]
