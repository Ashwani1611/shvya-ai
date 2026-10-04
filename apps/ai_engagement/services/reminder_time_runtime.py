from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as datetime_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils import timezone


_INSTALLED = False

_ISO_DATETIME_RE = re.compile(
    r"\b20\d{2}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:z|[+-]\d{2}:?\d{2})\b",
    re.IGNORECASE,
)
_OFFSET_RE = re.compile(r"\b(?:utc|gmt)\s*([+-])(\d{1,2})(?::?([0-5]\d))?(?![\d:])\b", re.I)
_NAMED_ZONE_RE = re.compile(
    r"\b((?:Africa|America|Antarctica|Arctic|Asia|Atlantic|Australia|Europe|Indian|Pacific|Etc)"
    r"/[a-z0-9_+-]+(?:/[a-z0-9_+-]+)?)\b", re.I,
)
_EXPLICIT_ZONE_RE = re.compile(r"\btime\s*zone\s*[:=]?\s*([a-z]+/[a-z0-9_+-]+(?:/[a-z0-9_+-]+)?)\b", re.I)

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
    r"\b(?:in|after)\s+(?P<amount>\d{1,3})\s*(?P<unit>minutes?|mins?|hours?|hrs?|days?|weeks?)\b",
    re.IGNORECASE,
)
_HINGLISH_RELATIVE_RE = re.compile(
    r"\b(?P<amount>\d{1,3})\s*(?P<unit>minutes?|mins?|ghant[ae]|din|haft[ae])\s+baad\b", re.I,
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
    text = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
               "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
               "ek": 1, "do": 2, "teen": 3, "char": 4, "paanch": 5}
    return re.sub(
        r"\b(one|two|three|four|five|six|seven|eight|nine|ten|ek|do|teen|char|paanch)"
        r"(?=\s+(?:minutes?|mins?|hours?|hrs?|days?|weeks?|ghant[ae]|din|haft[ae])\b)",
        lambda match: str(numbers[match[1]]), text,
    )


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
    local = re.search(
        r"(?:\b(subah|shaam|sham|dopahar|raat)\b|(?:सुबह|शाम|दोपहर|रात))\s*(?:ko\s+)?"
        r"(?P<hour>1[0-2]|0?[1-9])(?::(?P<minute>[0-5]\d))?\s*(?:baje|बजे)?", text,
    )
    if local:
        hour = int(local["hour"])
        morning = local[0].startswith(("subah", "सुबह"))
        if hour == 12:
            return None  # midnight/noon references require an explicit AM/PM.
        return (hour if morning else hour + 12), int(local["minute"] or 0)
    return None


def _parse_date(text: str, now) -> object | None:
    if "day after tomorrow" in text:
        return now.date() + timedelta(days=2)
    if "tomorrow" in text:
        return now.date() + timedelta(days=1)
    if "today" in text or "tonight" in text:
        return now.date()
    if re.search(r"\bparso[n]?\b|परसों", text):
        return now.date() + timedelta(days=2)
    if re.search(r"\bkal\b|कल", text):
        return now.date() + timedelta(days=1)
    if re.search(r"\baaj\b|आज", text):
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


def _requested_timezone(text: str, timezone_name: str | None):
    text = re.sub(r"https?://\S+", "", text, flags=re.I)
    named = _NAMED_ZONE_RE.search(text) or _EXPLICIT_ZONE_RE.search(text)
    if named:
        # IANA identifiers are case-sensitive; recover their spelling from the
        # original request rather than the normalized parsing text.
        try:
            return ZoneInfo(named[1])
        except ZoneInfoNotFoundError:
            return None
    offset = _OFFSET_RE.search(text)
    if offset:
        hours, minutes = int(offset[2]), int(offset[3] or 0)
        if hours > 14 or (hours == 14 and minutes):
            return None
        delta = timedelta(hours=hours, minutes=minutes)
        return datetime_timezone(delta if offset[1] == "+" else -delta)
    if re.search(r"\b(?:utc|gmt)\b", text, re.I):
        if re.search(r"\b(?:utc|gmt)\s*[+-]", text, re.I):
            return None
        return datetime_timezone.utc
    if re.search(r"\b(?:ist|india(?:n)?\s+time)\b", text, re.I):
        return ZoneInfo("Asia/Kolkata")
    # Ambiguous abbreviations must be clarified, never interpreted as the
    # server's timezone. Customers can give UTC offsets or IANA zone names.
    if re.search(r"\b(?:est|edt|cst|cdt|mst|mdt|pst|pdt|bst|cet|cest)\b", text, re.I):
        return None
    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            return None
    return timezone.get_current_timezone()


def _unambiguous_local_time(naive, target_timezone):
    first = naive.replace(tzinfo=target_timezone, fold=0)
    second = naive.replace(tzinfo=target_timezone, fold=1)
    # DST gaps and repeated wall-clock times need customer clarification.
    if first.utcoffset() != second.utcoffset():
        return None
    if first.astimezone(datetime_timezone.utc).astimezone(target_timezone).replace(tzinfo=None) != naive:
        return None
    return first


def parse_grounded_due_at(text: str, *, timezone_name: str | None = None) -> str | None:
    normalized = _clean(text)
    if not normalized:
        return None
    if re.search(r"\b(?:yesterday|last\s+(?:week|month)|beeta|beeti|tha|thi)\b", normalized):
        return None
    if re.search(r"\bor\b", normalized):
        return None
    iso_datetime = _ISO_DATETIME_RE.search(normalized)
    if iso_datetime:
        if len(_ISO_DATETIME_RE.findall(normalized)) > 1:
            return None
        try:
            due = datetime.fromisoformat(iso_datetime[0].upper().replace("Z", "+00:00"))
        except ValueError:
            return None
        return due.isoformat() if due > timezone.now() else None

    target_timezone = _requested_timezone(str(text or ""), timezone_name)
    if target_timezone is None:
        return None
    now = timezone.localtime(timezone.now(), target_timezone)
    # Two offered clock times/dates are alternatives, not an agreed due time.
    clock_text = _OFFSET_RE.sub("", normalized)
    times = _TIME_12H_RE.findall(clock_text) or _TIME_24H_RE.findall(clock_text)
    if len(times) > 1:
        return None
    relative = _RELATIVE_RE.search(normalized) or _HINGLISH_RELATIVE_RE.search(normalized)
    if relative:
        amount = int(relative.group("amount"))
        unit = relative.group("unit").casefold()
        if amount <= 0:
            return None
        if unit.startswith(("min", "minute")):
            delta = timedelta(minutes=amount)
        elif unit.startswith(("hour", "hr", "ghant")):
            delta = timedelta(hours=amount)
        elif unit.startswith(("week", "haft")):
            delta = timedelta(weeks=amount)
        else:
            delta = timedelta(days=amount)
        # Hours/minutes denote elapsed time, including across DST changes.
        return (now.astimezone(datetime_timezone.utc) + delta).astimezone(target_timezone).isoformat()

    # An offset is not the requested clock time ("6 October UTC+05:30").
    parsed_time = _parse_time(clock_text)
    if parsed_time is None:
        return None
    hour, minute = parsed_time

    target_date = _parse_date(normalized, now)
    if target_date is None:
        # A time alone, unsupported date, or invalid date does not authorize
        # inventing today/tomorrow for a customer callback.
        return None

    naive = datetime.combine(target_date, datetime.min.time()).replace(
        hour=hour,
        minute=minute,
    )
    aware = _unambiguous_local_time(naive, target_timezone)
    if aware is None or aware <= now:
        return None
    return aware.isoformat()
