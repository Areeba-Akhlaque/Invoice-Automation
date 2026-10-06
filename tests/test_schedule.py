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
    start, end = description_window("2026-09-08", today=date(2026, 9, 7))
    assert (start, end) == ("2026-08-24", "2026-09-07")


def test_description_window_never_reaches_past_today():
    """The automation runs before the issue date, so the raw window ends in the
    future — further ahead the earlier in the month it runs. Future days hold no
    entries, so the window should say what it actually covers."""
    start, end = description_window("2026-10-08", today=date(2026, 10, 5))
    assert (start, end) == ("2026-09-23", "2026-10-05")


def test_description_window_is_untouched_when_it_is_already_in_the_past():
    start, end = description_window("2026-09-08", today=date(2026, 9, 30))
    assert (start, end) == ("2026-08-24", "2026-09-07")


def test_a_wholly_future_window_is_left_alone_rather_than_inverted():
    """Capping a window that has not started yet would put its end before its
    start; better to hand back the real range and find nothing in it."""
    start, end = description_window("2026-12-08", today=date(2026, 10, 5))
    assert start < end
    assert (start, end) == ("2026-11-23", "2026-12-07")


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
