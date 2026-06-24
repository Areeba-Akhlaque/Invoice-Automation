"""ORCHESTRATION — coordinates the pipeline: resolve schedule windows, pull Kimai,
summarize descriptions, build the invoice plan, and (on commit) write the tab.

Reads the Directive layer, drives the Execution layer. No printing here — the
entry point (run.py) handles presentation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from execution.config import (
    google_credentials,
    load_gemini_key,
    load_kimai_env,
    load_roster,
    load_settings,
)
from execution.invoice import InvoicePlan, build_plan, write_plan
from execution.kimai import KimaiClient
from execution.sheets import SheetsClient
from execution.summarizer import GeminiSummarizer

from .schedule import (
    current_billing_period,
    description_window,
    issue_date_for,
    previous_half_month,
)


def parse_pairs(text: str | None) -> dict[str, float]:
    out: dict[str, float] = {}
    if not text:
        return out
    for chunk in text.split(","):
        if not chunk.strip():
            continue
        name, _, val = chunk.partition("=")
        out[name.strip()] = float(val.strip())
    return out


def _us_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{d.month}/{d.day}/{d.year}"


def build_descriptions(kimai: KimaiClient, begin: str, finish: str) -> tuple[dict, dict]:
    """Returns (descriptions, flags). Flags = {name: reason} for missing/repetitive
    Kimai descriptions (left empty + review note). Only fixed/kimai people who track
    in Kimai are checked. One batched Gemini call (quota-friendly)."""
    try:
        gem = GeminiSummarizer(load_gemini_key(), load_settings()["gemini"]["model"])
    except RuntimeError as e:
        print(f"  ! Descriptions skipped: {e}")
        return {}, {}

    by_user = kimai.descriptions_by_user(begin, finish)
    flags: dict[str, str] = {}
    work: list[tuple[str, list[str]]] = []
    for p in load_roster():
        if not (p.get("kimai_user_id") and p["hours_source"] in ("fixed", "kimai")):
            continue
        entries = by_user.get(p["kimai_user_id"]) or []
        uniq = {" ".join(e.split()).strip().lower() for e in entries if e.strip()}
        if not uniq:
            flags[p["name"]] = "missing"
        elif len(entries) >= 2 and len(uniq) == 1:
            flags[p["name"]] = "repetitive (same entry all period)"
        else:
            work.append((p["name"], entries))

    descriptions = gem.summarize_batch(work)
    if flags:
        print("  ! description flags (left empty + noted):", flags)
    return descriptions, flags


@dataclass
class InvoiceRun:
    plan: InvoicePlan
    sheets: SheetsClient
    period: tuple[str, str]
    issue: str
    hours_window: tuple[str, str] | None = None
    desc_window: tuple[str, str] | None = None
    log: list[str] = field(default_factory=list)


def prepare(opts) -> InvoiceRun:
    """Build the invoice plan from CLI/options (no write). opts attributes:
    start, end, invoice_date, hours_start, hours_end, cap, manual, passthrough,
    no_kimai, no_descriptions."""
    g = lambda k, d=None: getattr(opts, k, d)  # noqa: E731
    settings = load_settings()

    start, end = g("start"), g("end")
    if not start or not end:
        start, end = current_billing_period()
    issue = g("invoice_date") or issue_date_for(start, settings["schedule"]["issue_offset_days"])
    log = [f"Invoice period: {start} -> {end}   (issue {issue}, due = issue+7)"]

    sheets = SheetsClient(settings["google"]["spreadsheet_id"], google_credentials(settings))

    kimai_rows = None
    descriptions: dict = {}
    desc_flags: dict = {}
    hours_window = desc_win = None
    if not g("no_kimai"):
        env = load_kimai_env()
        kimai = KimaiClient(env["url"], env["token"], env["user"], env["verify_ssl"])
        ph_start, ph_end = previous_half_month(start)
        h_start = g("hours_start") or ph_start
        h_end = g("hours_end") or ph_end
        hours_window = (h_start, h_end)
        kimai_rows = kimai.hours_with_identity(f"{h_start}T00:00:00", f"{h_end}T23:59:59")
        log.append(f"Hours window (hourly estimate): {h_start} -> {h_end}  ({len(kimai_rows)} users)")

        if not g("no_descriptions"):
            d_start, d_end = description_window(issue)
            desc_win = (d_start, d_end)
            log.append(f"Descriptions window: {d_start} -> {d_end}")
            descriptions, desc_flags = build_descriptions(
                kimai, f"{d_start}T00:00:00", f"{d_end}T23:59:59"
            )

    plan = build_plan(
        sheets,
        kimai_rows,
        start_date=_us_date(start),
        end_date=_us_date(end),
        invoice_date=_us_date(issue),
        cap=g("cap"),
        manual_hours=parse_pairs(g("manual")),
        passthroughs=parse_pairs(g("passthrough")),
        descriptions=descriptions,
        desc_flags=desc_flags,
    )
    return InvoiceRun(plan, sheets, (start, end), issue, hours_window, desc_win, log)


def commit(run: InvoiceRun) -> str:
    """Write the prepared plan to a new sheet tab. Returns the tab name."""
    return write_plan(run.sheets, run.plan)
