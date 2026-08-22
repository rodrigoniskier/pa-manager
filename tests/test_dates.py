from datetime import date

from pa_manager.dates import classify_expiry, days_remaining, parse_expiry
from pa_manager.models import ProjectStatus


def test_parse_pythonanywhere_date_formats() -> None:
    expected = date(2026, 9, 22)
    assert parse_expiry("Tuesday 22 September 2026") == expected
    assert parse_expiry("Expires: September 22, 2026") == expected
    assert parse_expiry("This web app is running until Tuesday 22nd September 2026.") == expected
    assert parse_expiry("2026-09-22") == expected


def test_days_and_status_boundaries() -> None:
    today = date(2026, 8, 22)
    assert days_remaining(date(2026, 8, 29), today=today) == 7
    assert classify_expiry(date(2026, 8, 21), today=today) is ProjectStatus.EXPIRED
    assert classify_expiry(date(2026, 8, 29), today=today) is ProjectStatus.EXPIRING
    assert classify_expiry(date(2026, 8, 30), today=today) is ProjectStatus.ACTIVE
    assert classify_expiry(None, today=today) is ProjectStatus.UNKNOWN
