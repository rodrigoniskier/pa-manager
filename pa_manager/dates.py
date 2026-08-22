from __future__ import annotations

import re
from datetime import date, datetime

from pa_manager.models import ProjectStatus

_ORDINAL_SUFFIX = re.compile(r"(?<=\d)(st|nd|rd|th)\b", re.IGNORECASE)
_DATE_FORMATS = (
    "%A %d %B %Y",
    "%A, %d %B %Y",
    "%d %B %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y-%m-%d",
    "%d/%m/%Y",
)
_DATE_FRAGMENTS = (
    re.compile(
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+"
        r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}",
        re.IGNORECASE,
    ),
    re.compile(r"\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\s+\d{4}", re.IGNORECASE),
    re.compile(r"[A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}", re.IGNORECASE),
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\d{2}/\d{2}/\d{4}"),
)


def parse_expiry(value: str | None) -> date | None:
    if not value:
        return None
    cleaned = " ".join(value.replace("\xa0", " ").split()).strip(" .")
    cleaned = _ORDINAL_SUFFIX.sub("", cleaned)
    for prefix in ("Expires", "Expiry", "Run until", "Running until"):
        if cleaned.lower().startswith(prefix.lower()):
            cleaned = cleaned[len(prefix) :].lstrip(": ")
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    for pattern in _DATE_FRAGMENTS:
        match = pattern.search(cleaned)
        if match and match.group(0) != cleaned:
            parsed = parse_expiry(match.group(0))
            if parsed:
                return parsed
    return None


def days_remaining(expiry: date | None, *, today: date | None = None) -> int | None:
    if expiry is None:
        return None
    return (expiry - (today or date.today())).days


def classify_expiry(expiry: date | None, *, today: date | None = None) -> ProjectStatus:
    remaining = days_remaining(expiry, today=today)
    if remaining is None:
        return ProjectStatus.UNKNOWN
    if remaining < 0:
        return ProjectStatus.EXPIRED
    if remaining <= 7:
        return ProjectStatus.EXPIRING
    return ProjectStatus.ACTIVE
