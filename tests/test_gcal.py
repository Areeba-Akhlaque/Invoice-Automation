"""Calendar-sourced hours.

The numbers here must agree with the Apps Script that fills the calendar-sync
sheet, because that is what the client has been reconciling against. Same colour
map, same "end - start" duration, same all-day/cancelled skipping.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from execution.gcal import (
    DEFAULT_COLOR_LABEL,
    CalEvent,
    clean_description,
    label_for,
    parse_event,
    summarise,
)

TZ = ZoneInfo("America/Los_Angeles")
COLORS = {"2": "Ride Care", "4": "Pvragon", "7": "Lifestyle", "8": "Non-Attributable"}


def ev(h1, m1, h2, m2, *, project="Ride Care", name="Event", desc="", day=5):
    s = datetime(2026, 8, day, h1, m1, tzinfo=TZ)
    e = datetime(2026, 8, day, h2, m2, tzinfo=TZ)
    return CalEvent(s.date(), s, e, name, desc, (e - s) / timedelta(minutes=1), project)


# --------------------------------------------------------------------------
# Colour mapping
# --------------------------------------------------------------------------
def test_colour_id_maps_to_the_project_label():
    assert label_for("2", COLORS) == "Ride Care"
    assert label_for(2, COLORS) == "Ride Care"  # YAML may give an int


def test_uncoloured_and_unknown_colours_are_their_own_buckets():
    """They must never silently land in a billable project."""
    assert label_for(None, COLORS) == DEFAULT_COLOR_LABEL
    assert label_for("", COLORS) == DEFAULT_COLOR_LABEL
    assert label_for("99", COLORS) == "Unknown"


# --------------------------------------------------------------------------
# Turning an API item into a billable event
# --------------------------------------------------------------------------
def _api(**kw):
    base = {
        "summary": "Echo1 Lead Sync",
        "colorId": "2",
        "start": {"dateTime": "2026-08-05T10:00:00-07:00"},
        "end": {"dateTime": "2026-08-05T10:30:00-07:00"},
    }
    base.update(kw)
    return base


def test_parses_a_timed_event():
    e = parse_event(_api(), TZ, COLORS, True)
    assert (e.summary, e.minutes, e.project) == ("Echo1 Lead Sync", 30.0, "Ride Care")
    assert e.day.isoformat() == "2026-08-05"


def test_all_day_events_are_skipped():
    """Home / trips / OOO would otherwise add 1440 minutes each."""
    item = _api(start={"date": "2026-08-05"}, end={"date": "2026-08-06"})
    assert parse_event(item, TZ, COLORS, True) is None
    assert parse_event(item, TZ, COLORS, False) is not None  # unless configured otherwise


def test_cancelled_events_are_skipped():
    assert parse_event(_api(status="cancelled"), TZ, COLORS, True) is None


def test_event_times_are_converted_to_the_configured_timezone():
    """A UTC event must land on the local day, or it falls outside the period."""
    e = parse_event(
        _api(start={"dateTime": "2026-08-06T03:00:00Z"}, end={"dateTime": "2026-08-06T04:00:00Z"}),
        TZ, COLORS, True,
    )
    assert e.day.isoformat() == "2026-08-05"  # 8pm Pacific the previous day
    assert e.minutes == 60.0


def test_untitled_event_still_produces_a_line():
    assert parse_event(_api(summary=None), TZ, COLORS, True).summary == "(no title)"


# --------------------------------------------------------------------------
# Descriptions
# --------------------------------------------------------------------------
def test_meeting_boilerplate_is_stripped():
    assert clean_description("Real notes.\nJoin the meeting\nhttps://...") == "Real notes."
    assert clean_description("Notes here\n____\nMeeting ID: 1") == "Notes here"
    assert clean_description(None) == ""


def test_entry_uses_the_title_when_there_is_no_description():
    """Only 3 of 87 events in a sample fortnight had one, so titles carry it."""
    assert ev(9, 0, 10, 0, name="Data rules and integrity").entry == "Data rules and integrity"


def test_entry_combines_title_and_description_when_both_exist():
    e = ev(9, 0, 10, 0, name="Billing Discussion", desc="Reviewing the billing process")
    assert e.entry == "Billing Discussion — Reviewing the billing process"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------
def test_only_the_billable_colour_is_counted():
    got = summarise([ev(9, 0, 10, 0), ev(10, 0, 11, 0, project="Lifestyle")], "Ride Care")
    assert got.hours == 1.0
    assert got.event_count == 1
    assert got.totals == {"Ride Care": 1.0, "Lifestyle": 1.0}


def test_durations_are_summed_like_the_apps_script_and_overlap_reported_separately():
    """The sheet double-counts overlaps; we match it, but say so rather than
    silently differing from the number the client reconciles against."""
    got = summarise([ev(9, 0, 10, 0), ev(9, 30, 10, 30)], "Ride Care")
    assert got.hours == 2.0  # 60 + 60, as the sheet reports
    assert got.overlap_hours == 0.5  # 9:30-10:00


def test_adjacent_events_are_not_treated_as_overlapping():
    got = summarise([ev(9, 0, 10, 0), ev(10, 0, 11, 0)], "Ride Care")
    assert (got.hours, got.overlap_hours) == (2.0, 0.0)


def test_events_on_different_days_never_overlap():
    got = summarise([ev(9, 0, 17, 0, day=5), ev(9, 0, 17, 0, day=6)], "Ride Care")
    assert (got.hours, got.overlap_hours) == (16.0, 0.0)


def test_entries_are_deduplicated_in_order():
    """"Ride Care" appears as a title many times a fortnight."""
    got = summarise(
        [ev(9, 0, 10, 0, name="Ride Care"), ev(11, 0, 12, 0, name="Echo1 Lead Sync"),
         ev(13, 0, 14, 0, name="ride care")],
        "Ride Care",
    )
    assert got.entries == ["Ride Care", "Echo1 Lead Sync"]


def test_no_events_is_zero_not_an_error():
    got = summarise([], "Ride Care")
    assert (got.hours, got.entries, got.event_count) == (0.0, [], 0)


@pytest.mark.parametrize("project", ["Pvragon", "Nonexistent"])
def test_a_person_with_no_time_in_their_project_gets_zero(project):
    assert summarise([ev(9, 0, 10, 0)], project).hours == 0.0
