#!/usr/bin/env python
"""Connectivity / config sanity check. Run after filling .env (or setting env vars).

    python check_setup.py

Verifies Google + Kimai auth, checks the invoice tab layout still matches
settings.yaml, shows the latest invoice tab, and prints Kimai users so you can
fill the kimai_user_id fields in directive/roster.yaml.
"""
from __future__ import annotations

from execution.config import (
    _normalize_name,
    google_credentials,
    load_kimai_env,
    load_roster,
    load_settings,
)
from execution.gcal import DEFAULT_COLOR_LABEL, CalendarClient
from execution.invoice import latest_invoice_tab, validate_layout
from execution.kimai import KimaiClient
from execution.sheets import SheetsClient
from orchestration.schedule import current_billing_period, previous_half_month


def check_google() -> str | None:
    print("== Google Sheets ==")
    settings = load_settings()
    sheets = SheetsClient(settings["google"]["spreadsheet_id"], google_credentials(settings))
    titles = sheets.sheet_titles()
    print(f"  OK - opened spreadsheet with {len(titles)} tabs.")
    tab, num = latest_invoice_tab(titles, settings["invoice"]["prefix"])
    print(f"  Latest invoice tab: {tab}  (next will be #{num + 1:04d})")

    problems = validate_layout(sheets, tab, settings["layout"], settings["invoice"]["prefix"])
    if problems:
        print("  !! LAYOUT MISMATCH - a run would abort. Fix `layout:` in directive/settings.yaml:")
        for p in problems:
            print(f"     - {p}")
    else:
        lay = settings["layout"]
        print(
            f"  Layout OK: rows {lay['line_items_scan_rows']}, subtotal {lay['subtotal_cell']}, "
            f"discount {lay['discount_cell']}, total {lay['total_cell']}"
        )
    return tab


def check_roster_against_sheet(tab: str | None) -> None:
    """The single most expensive failure: a roster name that no longer matches
    column B is not billed at all."""
    print("\n== Roster vs sheet ==")
    if tab is None:
        print("  SKIPPED - no sheet access.")
        return
    settings = load_settings()
    lay = settings["layout"]
    r0, r1 = lay["line_items_scan_rows"]
    sheets = SheetsClient(settings["google"]["spreadsheet_id"], google_credentials(settings))
    names = sheets.get_values(f"'{tab}'!{lay['person_col']}{r0}:{lay['person_col']}{r1}")
    on_sheet = {_normalize_name(r[0]): r[0] for r in names if r and str(r[0]).strip()}

    roster = load_roster()
    active = [p for p in roster if p["active"]]
    print(f"  {len(roster)} laborers ({len(active)} active).")
    missing = [p["name"] for p in active if p["_key"] not in on_sheet]
    if missing:
        print(f"  !! ACTIVE but NOT on '{tab}' (would not be billed): {', '.join(missing)}")
    else:
        print(f"  All active people found on '{tab}'.")

    roster_keys = {p["_key"] for p in roster}
    extra = [v for k, v in on_sheet.items() if k not in roster_keys]
    if extra:
        print(f"  Rows on the sheet with no roster entry (pass-throughs etc.): {', '.join(extra)}")


def check_calendars() -> None:
    """Calendar-billed people: confirm the calendar is readable and, just as
    important, show how much time is UNCOLOURED — that time is not billed to
    anyone, and it has run at 33-53 h per half-month."""
    print("\n== Calendars ==")
    settings = load_settings()
    roster = [p for p in load_roster() if p["active"] and p["hours_source"] == "calendar"]
    if not roster:
        print("  No one is billed from a calendar.")
        return

    cfg = settings.get("calendar") or {}
    start, end = previous_half_month(current_billing_period()[0])
    client = CalendarClient(
        google_credentials(settings),
        timezone=cfg.get("timezone", "UTC"),
        color_map=cfg.get("color_map"),
        skip_all_day=cfg.get("skip_all_day_events", True),
    )
    print(f"  Window (previous complete half-month): {start} -> {end}")
    for p in roster:
        cal_id, project = p.get("calendar_id"), p.get("calendar_project")
        try:
            pull = client.collect(cal_id, project, start, end)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {p['name']} ({cal_id}): {str(e)[:140]}")
            continue
        print(f"  {p['name']}: {pull.hours:g} h billable as '{project}' ({pull.event_count} events)")
        if pull.overlap_hours >= 0.5:
            print(f"     ! {pull.overlap_hours:g} h counted twice (overlapping events)")
        uncoloured = pull.totals.get(DEFAULT_COLOR_LABEL, 0)
        if uncoloured:
            print(f"     ! {uncoloured:g} h uncoloured — not billed to any project")
        rest = {k: v for k, v in pull.totals.items() if k != project and v >= 1}
        if rest:
            print(f"     other colours: {rest}")


def check_kimai() -> None:
    print("\n== Kimai ==")
    try:
        env = load_kimai_env()
    except RuntimeError as e:
        print(f"  SKIPPED - {e}")
        return
    k = KimaiClient(env["url"], env["token"], env["user"], env["verify_ssl"])
    me = k.ping()
    print(f"  OK - authenticated as: {me.get('username') or me.get('alias')}")
    users = k.users()
    print(f"  {len(users)} Kimai users (use the id for kimai_user_id in roster.yaml):")
    for u in users:
        print(f"    id={u.get('id'):<4} {u.get('alias') or u.get('username')}")

    unmapped = [
        p["name"]
        for p in load_roster()
        if p["active"] and p["hours_source"] == "kimai" and not p.get("kimai_user_id")
    ]
    if unmapped:
        print(f"  ! kimai_user_id missing for: {', '.join(unmapped)}")


if __name__ == "__main__":
    tab = check_google()
    check_roster_against_sheet(tab)
    check_calendars()
    check_kimai()
    print("\nDone.")
