"""ORCHESTRATION — coordinates the pipeline: resolve schedule windows, pull Kimai,
summarize descriptions, build the invoice plan, and (on commit) write the tab.

Reads the Directive layer, drives the Execution layer. No printing here — the
entry point (run.py) handles presentation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from execution.config import (
    _normalize_name,
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


def build_projects_summary(sheets: SheetsClient, plan: InvoicePlan) -> str | None:
    """One paragraph for the "PROJECTS ON THIS INVOICE" cell, condensed from every
    person's project cell as it will appear on the new tab (fresh AI description,
    else text carried over from the template tab). The previous invoice's paragraph
    is passed as the style/length reference so it stays the same size. One extra
    Gemini call; returns None on failure (the carried-over paragraph then stays)."""
    settings = load_settings()
    try:
        gem = GeminiSummarizer(load_gemini_key(), settings["gemini"]["model"])
    except RuntimeError as e:
        print(f"  ! Projects summary skipped: {e}")
        return None

    lay = settings["layout"]
    r0, r1 = lay["line_items_scan_rows"]
    tab = plan.template_tab
    # Best-effort: any Sheets read failing must NOT abort the invoice run — the
    # cell simply keeps its carried-over text (mirrors the cap-carryover read).
    try:
        people = sheets.get_values(f"'{tab}'!{lay['person_col']}{r0}:{lay['person_col']}{r1}")
        projects = sheets.get_values(f"'{tab}'!{lay['project_col']}{r0}:{lay['project_col']}{r1}")
        hidden = sheets.hidden_rows(tab, r0, r1)
        vals = sheets.get_values(f"'{tab}'!{lay['project_description']}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! Projects summary skipped (sheet read failed): {str(e)[:100]}")
        return None

    name_to_row: dict[str, int] = {}
    carried: dict[str, str] = {}
    for i, prow in enumerate(people):
        name = prow[0] if prow else ""
        if not name:
            continue
        row = r0 + i
        key = _normalize_name(name)
        name_to_row[key] = row
        desc = projects[i][0] if i < len(projects) and projects[i] else ""
        if str(desc).strip():
            carried[key] = " ".join(str(desc).split()).strip()

    # Include a person's blurb iff their row will actually be VISIBLE with text on
    # the new tab — mirroring write_plan's billed-row logic so the paragraph matches
    # the Project column exactly (no stale/hidden people, no omitted 0-hour people).
    blurbs: list[str] = []
    for li in plan.line_items:
        key = _normalize_name(li.name)
        row = name_to_row.get(key)
        if row is None or key in plan.desc_flags:  # not on the sheet / cell blanked
            continue
        if li.hours is not None:
            visible = True                 # write_plan writes hours (and unhides)
        else:
            visible = row not in hidden     # hidden + unset -> Option A blanks it
        if not visible:
            continue
        desc = li.project_desc or carried.get(key, "")
        if desc:
            blurbs.append(desc)

    example = str(vals[0][0]) if vals and vals[0] else ""
    return gem.summarize_projects(blurbs, example)


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
    if descriptions:  # fresh per-person cells exist -> refresh the overall paragraph too
        plan.projects_summary = build_projects_summary(sheets, plan)
    return InvoiceRun(plan, sheets, (start, end), issue, hours_window, desc_win, log)


def commit(run: InvoiceRun) -> str:
    """Write the prepared plan to a new sheet tab. Returns the tab name."""
    return write_plan(run.sheets, run.plan)
