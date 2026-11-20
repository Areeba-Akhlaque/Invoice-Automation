#!/usr/bin/env python
"""One-off: re-summarize project descriptions on an invoice tab that already exists.

Two modes, both preview-by-default (--write to actually touch the cells):

  --person   ONE person, from a custom Kimai window. For when their normal window
             held nothing useful (all PTO, a late start, work under another project).

  --all      EVERYONE on the tab, from the period the tab bills hours for. For when
             the summarizer's house style changed and the tab should be rebuilt to
             it, which is otherwise a hand-edit per person. One batched Gemini call,
             the same one the real run makes, so it costs the same quota.

Neither mode touches the normal pipeline (run.py) — future invoices are unaffected.

    python tools/redo_description.py --person "Victor Cheung" \
        --from 2026-08-10 --to 2026-08-24 --tab DRC-0065
    python tools/redo_description.py --all --tab DRC-0070
    python tools/redo_description.py --all --write
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from execution.config import (
    _normalize_name,
    google_credentials,
    load_gemini_key,
    load_kimai_env,
    load_roster,
    load_settings,
)
from execution.entries import (
    clean_entries,
    compile_patterns,
    people_names,
    scrub_meeting_mentions,
    unsupported_terms,
)
from execution.invoice import latest_invoice_tab, resolve_layout
from execution.kimai import KimaiClient
from execution.sheets import SheetsClient
from execution.summarizer import GeminiSummarizer
from orchestration.orchestrator import (
    _resolve_projects,
    build_descriptions,
    pull_calendars,
)
from orchestration.schedule import current_billing_period, previous_half_month


def _tab_and_layout(sheets, settings, wanted: str | None):
    tab = wanted or latest_invoice_tab(sheets.sheet_titles(), settings["invoice"]["prefix"])[0]
    lay, notes = resolve_layout(sheets, tab, settings["layout"], settings["invoice"]["prefix"])
    for n in notes:
        print(f"  ! {n}")
    return tab, lay


def _rows_on_tab(sheets, tab: str, lay: dict) -> dict[str, int]:
    """Person key -> row, from column B of the tab itself (rows move between tabs)."""
    r0, r1 = lay["line_items_scan_rows"]
    scan = sheets.get_values(f"'{tab}'!{lay['person_col']}{r0}:{lay['person_col']}{r1}")
    return {
        _normalize_name(r[0]): r0 + i
        for i, r in enumerate(scan)
        if r and str(r[0]).strip()
    }


def redo_one(args, settings, sheets) -> None:
    key = _normalize_name(args.person)
    person = next((p for p in load_roster() if p["_key"] == key), None)
    if person is None:
        sys.exit(f"'{args.person}' is not in roster.yaml.")
    uid = person.get("kimai_user_id")
    if uid is None:
        sys.exit(f"'{person['name']}' has no kimai_user_id in roster.yaml.")

    env = load_kimai_env()
    kimai = KimaiClient(env["url"], env["token"], env["user"], env["verify_ssl"])
    by_user = kimai.descriptions_by_user(f"{args.begin}T00:00:00", f"{args.finish}T23:59:59")
    entries = by_user.get(uid) or []
    print(f"\nKimai window : {args.begin} -> {args.finish}")
    print(f"Person       : {person['name']}  (kimai id {uid})")
    print(f"Entries found: {len(entries)}")
    if not entries:
        sys.exit("No Kimai entries in that window — nothing to summarize.")

    # Same filtering as the real run. Without this, --person quietly wrote meeting
    # names into a cell while --all and the scheduled run stripped them out.
    cfg = settings.get("descriptions") or {}
    names = people_names(load_roster(), cfg.get("people"))
    entries, dropped = clean_entries(entries, compile_patterns(cfg.get("drop_segments")), names)
    if dropped:
        print(f"Dropped      : {dropped} recurring-meeting / time-off entries")
    if not entries:
        sys.exit("Every entry in that window was a recurring meeting or time off.")
    for e in GeminiSummarizer._dedupe(entries):
        print(f"   - {' '.join(e.split())[:110]}")

    gem = GeminiSummarizer(load_gemini_key(), settings["gemini"]["model"])
    desc = gem.summarize_batch([(person["name"], entries)]).get(person["name"])
    if not desc:
        sys.exit("Summarizer returned nothing (quota, rate limit or outage).")
    desc = scrub_meeting_mentions(desc, compile_patterns(cfg.get("scrub_phrases")), names) or desc
    print(f"\nNEW DESCRIPTION ({len(desc)} chars):\n  {desc}\n")
    invented = unsupported_terms(desc, entries)
    if invented:
        print(
            f"  !! The AI named {', '.join(invented)}, which appears in NONE of the "
            f"entries above. Do NOT write this without checking it.\n"
        )

    tab, lay = _tab_and_layout(sheets, settings, args.tab)
    row = _rows_on_tab(sheets, tab, lay).get(key)
    if row is None:
        sys.exit(f"'{person['name']}' not found in column {lay['person_col']} of '{tab}'.")

    cell = f"{lay['project_col']}{row}"
    current = sheets.get_values(f"'{tab}'!{cell}")
    current_text = current[0][0] if current and current[0] else "(empty)"
    print(f"Target       : '{tab}'!{cell}")
    print(f"CURRENT TEXT :\n  {current_text}\n")

    if not args.write:
        print("(preview only — re-run with --write to update the cell)")
        return
    sheets.batch_update_cells([(cell, desc)], tab)
    print(f"Updated '{tab}'!{cell}")


def redo_all(args, settings, sheets) -> None:
    """Rebuild every active person's description on an existing tab.

    The window is the one whose hours the tab bills, not one measured back from
    today: the invoice is raised in advance, so a tab for 16-30 Nov bills — and
    must describe — 1-15 Nov. Same rule as the live run.
    """
    roster = load_roster()
    tab, lay = _tab_and_layout(sheets, settings, args.tab)

    if args.begin and args.finish:
        window = (args.begin, args.finish)
    else:
        window = previous_half_month(current_billing_period()[0])
    print(f"\nTab          : {tab}")
    print(f"Window       : {window[0]} -> {window[1]}  (the period these hours were worked)")

    log: list[str] = []
    kimai = None
    kimai_project = None
    overrides: dict[int, set[int]] = {}
    if not args.no_kimai:
        env = load_kimai_env()
        kimai = KimaiClient(env["url"], env["token"], env["user"], env["verify_ssl"])
        want = (settings.get("kimai") or {}).get("project")
        if want:
            kimai_project = kimai.project_id(want)
            if kimai_project is None:
                sys.exit(f"kimai.project {want!r} is not a project in Kimai.")
        for p in roster:
            if p["active"] and p.get("kimai_projects") and p.get("kimai_user_id"):
                overrides[p["kimai_user_id"]] = _resolve_projects(kimai, p["kimai_projects"])

    calendar_entries: dict[str, list[str]] = {}
    if not args.no_calendar:
        _hours, calendar_entries = pull_calendars(
            google_credentials(settings), settings, roster, window, {}, log
        )

    descriptions, flags = build_descriptions(
        kimai,
        f"{window[0]}T00:00:00",
        f"{window[1]}T23:59:59",
        None,
        calendar_entries,
        log,
        kimai_project,
        overrides,
    )
    for line in log:
        print(line)
    if not descriptions:
        sys.exit("\nSummarizer returned nothing — cells left untouched.")

    rows = _rows_on_tab(sheets, tab, lay)
    r0, r1 = lay["line_items_scan_rows"]
    current = sheets.get_values(
        f"'{tab}'!{lay['project_col']}{r0}:{lay['project_col']}{r1}"
    )

    def existing(row: int) -> str:
        i = row - r0
        return current[i][0] if i < len(current) and current[i] else ""

    updates, missing = [], []
    print("\nNEW DESCRIPTIONS")
    print("-" * 100)
    for name, desc in descriptions.items():
        row = rows.get(_normalize_name(name))
        if row is None:
            missing.append(name)
            continue
        cell = f"{lay['project_col']}{row}"
        was = existing(row)
        print(f"\n{name}  ->  {cell}   ({len(was)} chars -> {len(desc)} chars)")
        print(f"  OLD: {was or '(empty)'}")
        print(f"  NEW: {desc}")
        updates.append((cell, desc))

    if missing:
        print(f"\n  ! not on '{tab}', skipped: {', '.join(missing)}")
    if flags:
        print("\n  ! flagged, cell left as it is (no usable entries this period):")
        for name, reason in flags.items():
            print(f"    {name}: {reason}")
    carried = [p["name"] for p in roster if p["active"] and p.get("carry_description")]
    if carried:
        print(f"\n  (carry_description in roster.yaml, left alone: {', '.join(carried)})")

    if not updates:
        print("\nNothing to write.")
        return
    if not args.write:
        print(f"\n(preview only — re-run with --write to update {len(updates)} cells on '{tab}')")
        return
    sheets.batch_update_cells(updates, tab)
    print(f"\nUpdated {len(updates)} project cells on '{tab}'.")


def main() -> None:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Re-summarize invoice project descriptions.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--person", help="Name as it appears in roster.yaml")
    mode.add_argument("--all", action="store_true",
                      help="Rebuild every active person's description on the tab")
    ap.add_argument("--from", dest="begin", default=None, help="Window start YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="finish", default=None, help="Window end YYYY-MM-DD (inclusive)")
    ap.add_argument("--tab", default=None, help="Invoice tab (default: latest DRC-####)")
    ap.add_argument("--no-kimai", action="store_true", help="--all: skip Kimai")
    ap.add_argument("--no-calendar", action="store_true", help="--all: skip Google Calendar")
    ap.add_argument("--write", action="store_true", help="Write the cells (default: preview only)")
    args = ap.parse_args()

    if args.person and not (args.begin and args.finish):
        ap.error("--person needs both --from and --to")

    settings = load_settings()
    sheets = SheetsClient(settings["google"]["spreadsheet_id"], google_credentials(settings))
    if args.all:
        redo_all(args, settings, sheets)
    else:
        redo_one(args, settings, sheets)


if __name__ == "__main__":
    main()
