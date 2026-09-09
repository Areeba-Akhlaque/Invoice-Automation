"""Billing-calendar maths. Pure functions, no I/O — the cheapest safety net there is."""
from datetime import date

import pytest

from orchestration.schedule import (
    current_billing_period,
    description_window,
    is_billing_day,
    issue_date_for,
    previous_half_month,
)


@pytest.mark.parametrize(
    "today,expected",
    [
        (date(2026, 9, 1), ("2026-09-01", "2026-09-15")),
        (date(2026, 9, 7), ("2026-09-01", "2026-09-15")),
        (date(2026, 9, 15), ("2026-09-01", "2026-09-15")),
        (date(2026, 9, 16), ("2026-09-16", "2026-09-30")),
        (date(2026, 9, 22), ("2026-09-16", "2026-09-30")),
        (date(2026, 2, 20), ("2026-02-16", "2026-02-28")),  # short month
        (date(2028, 2, 20), ("2028-02-16", "2028-02-29")),  # leap year
        (date(2026, 12, 31), ("2026-12-16", "2026-12-31")),
    ],
)
def test_current_billing_period(today, expected):
    assert current_billing_period(today) == expected


@pytest.mark.parametrize(
    "start,expected",
    [
        ("2026-09-01", ("2026-08-16", "2026-08-31")),
        ("2026-09-16", ("2026-09-01", "2026-09-15")),
        ("2026-01-01", ("2025-12-16", "2025-12-31")),  # year rollover
        ("2026-03-01", ("2026-02-16", "2026-02-28")),
        ("2028-03-01", ("2028-02-16", "2028-02-29")),  # leap year
    ],
)
def test_previous_half_month(start, expected):
    assert previous_half_month(start) == expected


def test_issue_date_and_due():
    assert issue_date_for("2026-09-01") == "2026-09-08"
    assert issue_date_for("2026-09-16") == "2026-09-23"
    assert issue_date_for("2026-12-16") == "2026-12-23"


def test_description_window_is_fifteen_days_ending_the_day_before_issue():
    start, end = description_window("2026-09-08")
    assert (start, end) == ("2026-08-24", "2026-09-07")


def test_is_billing_day():
    assert is_billing_day([7, 22], date(2026, 9, 7))
    assert is_billing_day([7, 22], date(2026, 9, 22))
    assert not is_billing_day([7, 22], date(2026, 9, 8))
