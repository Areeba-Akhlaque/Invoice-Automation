#!/usr/bin/env python
"""One-off: re-summarize ONE person's project description from a custom Kimai
window and write it into their row on an existing invoice tab.

Does not touch the normal pipeline (run.py) — future invoices behave exactly as
before. Preview by default; --write to actually update the cell.

    python tools/redo_description.py --person "Victor Cheung" \
        --from 2026-08-10 --to 2026-08-24 --tab DRC-0065
    python tools/redo_description.py ... --write
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
from execution.invoice import latest_invoice_tab
from execution.kimai import KimaiClient
from execution.sheets import SheetsClient
from execution.summarizer import GeminiSummarizer


def main() -> None:
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Re-summarize one person's invoice description.")
    ap.add_argument("--person", required=True, help="Name as it appears in roster.yaml")
    ap.add_argument("--from", dest="begin", required=True, help="Window start YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="finish", required=True, help="Window end YYYY-MM-DD (inclusive)")
    ap.add_argument("--tab", default=None, help="Invoice tab (default: latest DRC-####)")
    ap.add_argument("--write", action="store_true", help="Write the cell (default: preview only)")
    args = ap.parse_args()

    settings = load_settings()
    lay = settings["layout"]
    key = _normalize_name(args.person)

    person = next((p for p in load_roster() if p["_key"] == key), None)
    if person is None:
        sys.exit(f"'{args.person}' is not in roster.yaml.")
    uid = person.get("kimai_user_id")
    if uid is None:
        sys.exit(f"'{person['name']}' has no kimai_user_id in roster.yaml.")

    # --- Kimai entries for the custom window ---
    env = load_kimai_env()
    kimai = KimaiClient(env["url"], env["token"], env["user"], env["verify_ssl"])
    by_user = kimai.descriptions_by_user(f"{args.begin}T00:00:00", f"{args.finish}T23:59:59")
    entries = by_user.get(uid) or []
    print(f"\nKimai window : {args.begin} -> {args.finish}")
    print(f"Person       : {person['name']}  (kimai id {uid})")
    print(f"Entries found: {len(entries)}")
    if not entries:
        sys.exit("No Kimai entries in that window — nothing to summarize.")
    for e in GeminiSummarizer._dedupe(entries):
        print(f"   - {' '.join(e.split())[:110]}")

    # --- Summarize in the same house style as the normal run ---
    gem = GeminiSummarizer(load_gemini_key(), settings["gemini"]["model"])
    desc = gem.summarize_batch([(person["name"], entries)]).get(person["name"])
    if not desc:
        sys.exit("Summarizer returned nothing.")
    print(f"\nNEW DESCRIPTION ({len(desc)} chars):\n  {desc}\n")

    # --- Locate the row on the invoice tab ---
    sheets = SheetsClient(settings["google"]["spreadsheet_id"], google_credentials(settings))
    tab = args.tab or latest_invoice_tab(sheets.sheet_titles(), settings["invoice"]["prefix"])[0]
    r0, r1 = lay["line_items_scan_rows"]
    scan = sheets.get_values(f"'{tab}'!{lay['person_col']}{r0}:{lay['person_col']}{r1}")
    row = next((r0 + i for i, r in enumerate(scan) if r and _normalize_name(r[0]) == key), None)
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


if __name__ == "__main__":
    main()
