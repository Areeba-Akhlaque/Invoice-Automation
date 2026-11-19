"""Billing-calendar maths. Pure functions, no I/O — the cheapest safety net there is."""
from datetime import date

import pytest

from orchestration.schedule import (
    current_billing_period,
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


@pytest.mark.parametrize("run_day", [date(2026, 10, 5), date(2026, 10, 20)])
def test_the_described_period_is_the_period_whose_hours_are_billed(run_day):
    """The invoice is raised in advance: one built on 5 Oct bills 1-15 Oct but the
    hours come from 16-30 Sep, so the descriptions must cover 16-30 Sep too. A
    window counted back from the issue date drifted off that period and missed
    real work, so there is no separate description window any more."""
    period_start, _period_end = current_billing_period(run_day)
    hours_window = previous_half_month(period_start)
    # What orchestrator.prepare() now uses for both hours and descriptions.
    assert hours_window[1] < run_day.isoformat()   # the worked period is complete
    assert hours_window == previous_half_month(period_start)


@pytest.mark.parametrize("run_day,expected_period", [
    (date(2026, 10, 5), ("2026-10-01", "2026-10-15")),
    (date(2026, 10, 20), ("2026-10-16", "2026-10-31")),
])
def test_the_new_run_days_land_on_the_right_period(run_day, expected_period):
    """Moved from the 7th/22nd to the 5th/20th: each still falls inside the half
    it is meant to bill, and the hours window behind it is already complete."""
    assert current_billing_period(run_day) == expected_period
    _hours_start, hours_end = previous_half_month(expected_period[0])
    assert hours_end < run_day.isoformat()


def test_is_billing_day_matches_the_configured_run_days():
    assert is_billing_day([5, 20], date(2026, 10, 5))
    assert is_billing_day([5, 20], date(2026, 10, 20))
    assert not is_billing_day([5, 20], date(2026, 10, 7))


def test_is_billing_day():
    assert is_billing_day([7, 22], date(2026, 9, 7))
    assert is_billing_day([7, 22], date(2026, 9, 22))
    assert not is_billing_day([7, 22], date(2026, 9, 8))
