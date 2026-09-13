from __future__ import annotations

import re
from datetime import datetime, timedelta

from django.utils import timezone


_INSTALLED = False

_TIME_12H_RE = re.compile(
    r"\b(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*(?P<ampm>a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
_TIME_24H_RE = re.compile(r"\b(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)\b")
_ISO_DATE_RE = re.compile(
    r"\b(?P<year>20\d{2})[-/](?P<month>0?[1-9]|1[0-2])[-/](?P<day>0?[1-9]|[12]\d|3[01])\b"
)
_DMY_DATE_RE = re.compile(
    r"\b(?P<day>0?[1-9]|[12]\d|3[01])[-/](?P<month>0?[1-9]|1[0-2])(?:[-/](?P<year>20\d{2}))?\b"
)
_RELATIVE_RE = re.compile(
    r"\bin\s+(?P<amount>\d{1,3})\s*(?P<unit>minutes?|mins?|hours?|hrs?)\b",
    re.IGNORECASE,
)
_MONTH_DATE_RE = re.compile(
    r"\b(?:(?P<day1>0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?\s+(?P<month1>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)|(?P<month2>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+(?P<day2>0?[1-9]|[12]\d|3[01])(?:st|nd|rd|th)?)(?:[,\s]+(?P<year>20\d{2}))?\b",
    re.IGNORECASE,
)

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
_MONTHS = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _month_number(value: str | None) -> int | None:
    if not value:
        return None
    normalized = value.casefold().rstrip(".")
    return _MONTHS.get(normalized)


def _parse_time(text: str) -> tuple[int, int] | None:
    match12 = _TIME_12H_RE.search(text)
    if match12:
        hour = int(match12.group("hour"))
        minute = int(match12.group("minute") or 0)
        ampm = match12.group("ampm").replace(".", "").casefold()
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        return hour, minute

    match24 = _TIME_24H_RE.search(text)
    if match24:
        return int(match24.group("hour")), int(match24.group("minute"))
    return None


def _parse_date(text: str, now) -> object | None:
    if "day after tomorrow" in text:
        return now.date() + timedelta(days=2)
    if "tomorrow" in text:
        return now.date() + timedelta(days=1)
    if "today" in text or "tonight" in text:
        return now.date()

    iso = _ISO_DATE_RE.search(text)
    if iso:
        try:
            return datetime(
                int(iso.group("year")),
                int(iso.group("month")),
                int(iso.group("day")),
            ).date()
        except ValueError:
            return None

    dmy = _DMY_DATE_RE.search(text)
    if dmy:
        year = int(dmy.group("year") or now.year)
        try:
            candidate = datetime(year, int(dmy.group("month")), int(dmy.group("day"))).date()
        except ValueError:
            return None
        if not dmy.group("year") and candidate < now.date():
            try:
                candidate = candidate.replace(year=year + 1)
            except ValueError:
                return None
        return candidate

    month_date = _MONTH_DATE_RE.search(text)
    if month_date:
        day = int(month_date.group("day1") or month_date.group("day2"))
        month = _month_number(month_date.group("month1") or month_date.group("month2"))
        year = int(month_date.group("year") or now.year)
        if month is None:
            return None
        try:
            candidate = datetime(year, month, day).date()
        except ValueError:
            return None
        if not month_date.group("year") and candidate < now.date():
            try:
                candidate = candidate.replace(year=year + 1)
            except ValueError:
                return None
        return candidate

    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b(?:next\s+)?{name}\b", text):
            days = (weekday - now.weekday()) % 7
            if days == 0 or f"next {name}" in text:
                days = 7 if days == 0 else days
            return now.date() + timedelta(days=days)

    return None


def parse_grounded_due_at(text: str) -> str | None:
    normalized = _clean(text)
    if not normalized:
        return None

    now = timezone.localtime(timezone.now())
    relative = _RELATIVE_RE.search(normalized)
    if relative:
        amount = int(relative.group("amount"))
        unit = relative.group("unit").casefold()
        delta = timedelta(minutes=amount) if unit.startswith(("min", "minute")) else timedelta(hours=amount)
        return (now + delta).isoformat()

    parsed_time = _parse_time(normalized)
    if parsed_time is None:
        return None
    hour, minute = parsed_time

    target_date = _parse_date(normalized, now)
    if target_date is None:
        target_date = now.date()
        candidate = timezone.make_aware(
            datetime.combine(target_date, datetime.min.time()).replace(
                hour=hour,
                minute=minute,
            ),
            timezone.get_current_timezone(),
        )
        if candidate <= now:
            target_date = target_date + timedelta(days=1)

    naive = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour,
        minute=minute,
    )
    aware = timezone.make_aware(naive, timezone.get_current_timezone())
    if aware <= now and target_date == now.date():
        return None
    return aware.isoformat()


def install_reminder_time_runtime() -> None:
    global _INSTALLED
    if _INSTALLED:
        return

    from apps.ai_engagement.services import qualification_crm_action_runtime as module

    module._parse_grounded_due_at = parse_grounded_due_at
    _INSTALLED = True
