#!/usr/bin/env python
"""DRC invoice automation — entry point (Directive -> Orchestration -> Execution).

Examples:
    # Preview the next invoice (auto-detects period from today), nothing written:
    python run.py

    # Specific period, with manual hours + a pass-through, then write:
    python run.py --start 2026-07-01 --end 2026-07-15 \
        --manual "James Hereford=116,Bradd Schofield=50" \
        --passthrough "Google Cloud Platform=1642.46" --write

    # Scheduled (cron / GitHub Actions): only runs on the 7th & 22nd, writes silently:
    python run.py --auto --write --yes
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from execution.config import load_settings
from orchestration.orchestrator import commit, prepare
from orchestration.schedule import is_billing_day


def _money(x: float) -> str:
    return f"-${abs(x):,.2f}" if x < 0 else f"${x:,.2f}"


def _print_preview(plan) -> None:
    print(f"\n  INVOICE {plan.invoice_number}   (template: {plan.template_tab})")
    print(f"  Period: {plan.start_date} -> {plan.end_date}   Invoice date: {plan.invoice_date}")
    print("  " + "-" * 72)
    print(f"  {'PERSON':<24}{'SRC':<8}{'HOURS':>8}{'RATE':>10}{'AMOUNT':>14}")
    print("  " + "-" * 72)
    for li in plan.line_items:
        hrs = "" if li.hours is None else f"{li.hours:g}"
        amt = "" if li.amount is None else f"${li.amount:,.2f}"
        print(f"  {li.name:<24}{li.source:<8}{hrs:>8}{li.bill_rate:>10g}{amt:>14}")
    if plan.passthroughs:
        print("  " + "-" * 72)
        for name, amt in plan.passthroughs.items():
            print(f"  {name:<40}{'passthru':<8}{('$%0.2f' % amt):>22}")
    print("  " + "-" * 72)
    print(f"  {'Subtotal:':<54}{_money(plan.subtotal):>20}")
    if plan.cap is not None:
        print(f"  {'Discount cell (=Subtotal-Cap):':<54}{_money(plan.discount):>20}")
    print(f"  {'TOTAL:':<54}{_money(plan.total):>20}")
    descs = [(li.name, li.project_desc) for li in plan.line_items if li.project_desc]
    if descs:
        print("\n  AI descriptions:")
        for name, d in descs:
            print(f"    {name}: {d[:90]}{'...' if len(d) > 90 else ''}")
    if plan.desc_flags:
        print("\n  ! Description FLAGS (left empty + cell note for review):")
        for name, reason in plan.desc_flags.items():
            print(f"    {name}: {reason}")
    if plan.warnings:
        print("\n  ! Warnings:")
        for w in plan.warnings:
            print(f"    - {w}")
    print()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows cp1252 safety
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Generate the next DRC invoice tab.")
    ap.add_argument("--start", default=None, help="Period start YYYY-MM-DD (default: auto from today)")
    ap.add_argument("--end", default=None, help="Period end YYYY-MM-DD (default: auto from today)")
    ap.add_argument("--invoice-date", default=None, help="YYYY-MM-DD (default: period start + 7)")
    ap.add_argument("--hours-start", default=None, help="Kimai hours window start (default: previous half-month)")
    ap.add_argument("--hours-end", default=None, help="Kimai hours window end (default: previous half-month)")
    ap.add_argument("--cap", type=float, default=None, help="Agreed TOTAL; discount auto-fills the gap")
    ap.add_argument("--manual", default=None, help='Manual hours, e.g. "James Hereford=116,Bradd Schofield=50"')
    ap.add_argument("--passthrough", default=None, help='Pass-through $, e.g. "Google Cloud Platform=1642.46"')
    ap.add_argument("--no-kimai", action="store_true", help="Skip Kimai (kimai people get 0 hours)")
    ap.add_argument("--no-descriptions", action="store_true", help="Skip AI project descriptions")
    ap.add_argument("--write", action="store_true", help="Write the new tab (default: preview only)")
    ap.add_argument("--yes", action="store_true", help="Skip the write confirmation (for scheduled runs)")
    ap.add_argument("--auto", action="store_true", help="Only run on configured run days (7th/22nd); else exit")
    args = ap.parse_args()

    if args.auto:
        run_days = load_settings()["schedule"]["run_days"]
        if not is_billing_day(run_days):
            print(f"{date.today().isoformat()}: not a billing day {run_days}. Exiting.")
            return

    run = prepare(args)
    for line in run.log:
        print(line)
    _print_preview(run.plan)

    if not args.write:
        print("(preview only — re-run with --write to create the tab)")
        return

    if not args.yes:
        if input(f"Write new tab '{run.plan.invoice_number}'? [y/N] ").strip().lower() != "y":
            print("Aborted — nothing written.")
            return
    print(f"Created tab: {commit(run)}")


if __name__ == "__main__":
    main()
