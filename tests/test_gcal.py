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
    clip_to_window,
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


def test_double_booked_time_is_billed_once():
    """A call sitting inside a longer block is the same minutes, not extra ones.
    The Apps Script sums durations and bills them twice; we bill the real elapsed
    time and report the difference so the two can be reconciled."""
    got = summarise([ev(9, 0, 10, 0), ev(9, 30, 10, 30)], "Ride Care")
    assert got.hours == 1.5  # 09:00-10:30 actually elapsed
    assert got.overlap_hours == 0.5  # 09:30-10:00, excluded from `hours`


def test_an_event_wholly_inside_another_adds_nothing():
    got = summarise([ev(9, 0, 17, 0), ev(10, 0, 11, 0)], "Ride Care")
    assert got.hours == 8.0
    assert got.overlap_hours == 1.0


def test_no_cap_is_applied_however_long_the_day_runs():
    """Policy decision: every hour on the calendar is billable, no daily limit."""
    got = summarise([ev(6, 0, 23, 30)], "Ride Care")
    assert got.hours == 17.5


def test_adjacent_events_are_not_treated_as_overlapping():
    got = summarise([ev(9, 0, 10, 0), ev(10, 0, 11, 0)], "Ride Care")
    assert (got.hours, got.overlap_hours) == (2.0, 0.0)


def test_events_on_different_days_never_overlap():
    got = summarise([ev(9, 0, 17, 0, day=5), ev(9, 0, 17, 0, day=6)], "Ride Care")
    assert (got.hours, got.overlap_hours) == (16.0, 0.0)


def test_entries_are_deduplicated_in_order():
    """A recurring meeting appears many times a fortnight; the AI needs it once."""
    got = summarise(
        [ev(9, 0, 10, 0, name="Echo1 Lead Sync"), ev(11, 0, 12, 0, name="Data Migration"),
         ev(13, 0, 14, 0, name="echo1 lead sync")],
        "Ride Care",
    )
    assert got.entries == ["Echo1 Lead Sync", "Data Migration"]


def test_blocks_titled_after_the_project_are_not_description_material():
    """James's calendar is full of blocks literally titled "Ride Care"; on a Ride
    Care invoice that word is noise. They still count toward the hours."""
    got = summarise(
        [ev(9, 0, 10, 0, name="Ride Care"), ev(10, 0, 11, 0, name="Ride CAre"),
         ev(11, 0, 12, 0, name="Echo1 Lead Sync")],
        "Ride Care",
    )
    assert got.entries == ["Echo1 Lead Sync"]
    assert got.hours == 3.0  # all three are still billed


def test_a_project_titled_block_is_kept_when_it_carries_a_description():
    got = summarise([ev(9, 0, 10, 0, name="Ride Care", desc="Cutover planning")], "Ride Care")
    assert got.entries == ["Ride Care — Cutover planning"]


def test_no_events_is_zero_not_an_error():
    got = summarise([], "Ride Care")
    assert (got.hours, got.entries, got.event_count) == (0.0, [], 0)


@pytest.mark.parametrize("project", ["Pvragon", "Nonexistent"])
def test_a_person_with_no_time_in_their_project_gets_zero(project):
    assert summarise([ev(9, 0, 10, 0)], project).hours == 0.0


# --------------------------------------------------------------------------
# Clipping to the billed window
# --------------------------------------------------------------------------
WIN_S = datetime(2026, 9, 16, 0, 0, tzinfo=TZ)
WIN_E = datetime(2026, 9, 30, 23, 59, 59, 999999, tzinfo=TZ)


def _at(d1, h1, m1, d2, h2, m2):
    s = datetime(2026, 9, d1, h1, m1, tzinfo=TZ)
    e = datetime(2026, 9, d2, h2, m2, tzinfo=TZ)
    return CalEvent(s.date(), s, e, "Block", "", (e - s) / timedelta(minutes=1), "Ride Care")


def test_an_event_inside_the_window_is_untouched():
    e = _at(20, 9, 0, 20, 17, 0)
    assert clip_to_window(e, WIN_S, WIN_E) is e


def test_a_block_starting_before_the_window_is_trimmed():
    """It began the night before the period; those hours belong to the previous
    invoice, not this one."""
    got = clip_to_window(_at(15, 22, 0, 16, 2, 0), WIN_S, WIN_E)
    assert got.start == WIN_S
    assert got.minutes == 120.0  # only the 2 hours after midnight on the 16th


def test_a_block_running_past_the_window_is_trimmed():
    got = clip_to_window(_at(30, 22, 0, 30, 23, 0), WIN_S, WIN_E)
    assert got.minutes == 60.0
    longer = clip_to_window(_at(30, 23, 0, 30, 23, 59), WIN_S, WIN_E)
    assert longer.minutes == 59.0


def test_an_event_wholly_outside_the_window_is_dropped():
    assert clip_to_window(_at(14, 9, 0, 14, 17, 0), WIN_S, WIN_E) is None  # before
    after = datetime(2026, 10, 2, 9, 0, tzinfo=TZ)
    later = datetime(2026, 10, 2, 17, 0, tzinfo=TZ)
    e = CalEvent(after.date(), after, later, "Block", "", 480.0, "Ride Care")
    assert clip_to_window(e, WIN_S, WIN_E) is None  # after
