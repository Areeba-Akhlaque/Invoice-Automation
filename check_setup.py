#!/usr/bin/env python
"""Connectivity / config sanity check. Run after filling .env (or setting env vars).

    python check_setup.py

Verifies Google + Kimai auth, shows the latest invoice tab, and prints Kimai users
so you can fill the kimai_user_id fields in directive/roster.yaml.
"""
from __future__ import annotations

from execution.config import google_credentials, load_kimai_env, load_roster, load_settings
from execution.invoice import latest_invoice_tab
from execution.kimai import KimaiClient
from execution.sheets import SheetsClient


def check_google() -> None:
    print("== Google Sheets ==")
    settings = load_settings()
    sheets = SheetsClient(settings["google"]["spreadsheet_id"], google_credentials(settings))
    titles = sheets.sheet_titles()
    print(f"  OK - opened spreadsheet with {len(titles)} tabs.")
    tab, num = latest_invoice_tab(titles, settings["invoice"]["prefix"])
    print(f"  Latest invoice tab: {tab}  (next will be #{num + 1:04d})")


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


def check_roster() -> None:
    print("\n== Roster ==")
    roster = load_roster()
    missing = [p["name"] for p in roster if p["hours_source"] == "kimai" and not p.get("kimai_user_id")]
    print(f"  {len(roster)} laborers loaded.")
    print("  ! kimai_user_id missing for: " + ", ".join(missing) if missing else "  All Kimai people mapped.")


if __name__ == "__main__":
    check_google()
    check_kimai()
    check_roster()
    print("\nDone.")
