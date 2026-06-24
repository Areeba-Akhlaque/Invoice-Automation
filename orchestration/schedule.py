"""ORCHESTRATION — billing calendar logic (pure functions, no I/O).

Cherry's advance-billing model: bill 1-15 and 16-EOM; issue = period start + 7;
due = period end. The automation runs the day before issue (7th & 22nd).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta


def current_billing_period(today: date | None = None) -> tuple[str, str]:
    """The half-month period being invoiced, from the run date. ISO (start, end)."""
    t = today or date.today()
    if t.day <= 15:
        return f"{t.year}-{t.month:02d}-01", f"{t.year}-{t.month:02d}-15"
    last = calendar.monthrange(t.year, t.month)[1]
    return f"{t.year}-{t.month:02d}-16", f"{t.year}-{t.month:02d}-{last:02d}"


def issue_date_for(start_iso: str, offset_days: int = 7) -> str:
    """Invoice issue date = period start + offset (1-15 -> 8th, 16-30 -> 23rd)."""
    d = datetime.strptime(start_iso, "%Y-%m-%d").date() + timedelta(days=offset_days)
    return d.isoformat()


def previous_half_month(start_iso: str) -> tuple[str, str]:
    """Complete half-month BEFORE the invoice period (hourly estimate basis)."""
    s = datetime.strptime(start_iso, "%Y-%m-%d").date()
    if s.day >= 16:
        return f"{s.year}-{s.month:02d}-01", f"{s.year}-{s.month:02d}-15"
    prev_last = s - timedelta(days=1)  # last day of previous month
    return (
        f"{prev_last.year}-{prev_last.month:02d}-16",
        f"{prev_last.year}-{prev_last.month:02d}-{prev_last.day:02d}",
    )


def description_window(issue_iso: str) -> tuple[str, str]:
    """Kimai window for descriptions = [issue - 15, issue - 1] (e.g. 16-30 -> Jun 8-22)."""
    d = datetime.strptime(issue_iso, "%Y-%m-%d").date()
    return (d - timedelta(days=15)).isoformat(), (d - timedelta(days=1)).isoformat()


def is_billing_day(run_days: list[int], today: date | None = None) -> bool:
    return (today or date.today()).day in set(run_days)
