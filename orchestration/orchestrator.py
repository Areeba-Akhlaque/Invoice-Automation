"""ORCHESTRATION — coordinates the pipeline: resolve schedule windows, pull Kimai,
summarize descriptions, build the invoice plan, and (on commit) write the tab.

Reads the Directive layer, drives the Execution layer. Messages go into
InvoiceRun.log — the entry point (run.py) handles presentation.
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


def parse_pairs(text: str | None, *, what: str = "value") -> dict[str, float]:
    """'Name=12.5,Other=3' -> {'Name': 12.5, 'Other': 3.0}, with a readable error."""
    out: dict[str, float] = {}
    if not text:
        return out
    for chunk in text.split(","):
        if not chunk.strip():
            continue
        name, sep, val = chunk.partition("=")
        name, val = name.strip(), val.strip()
        if not sep or not name or not val:
            raise ValueError(f"bad --{what} entry {chunk.strip()!r} — expected \"Name=number\"")
        try:
            out[name] = float(val)
        except ValueError:
            raise ValueError(f"bad --{what} entry {chunk.strip()!r} — {val!r} is not a number") from None
    return out


def parse_desc_windows(text: str | None) -> dict[str, tuple[str, str]]:
    """'Victor Cheung=2026-08-10:2026-08-24' -> {'victor cheung': ('2026-08-10','2026-08-24')}.

    Per-person override of the Kimai window used for the AI project description.
    Everyone else keeps the default window; this changes nothing else.
    """
    out: dict[str, tuple[str, str]] = {}
    if not text:
        return out
    for chunk in text.split(","):
        if not chunk.strip():
            continue
        name, sep, span = chunk.partition("=")
        start, _, end = span.partition(":")
        name, start, end = name.strip(), start.strip(), end.strip()
        if not (sep and name and start and end):
            raise ValueError(
                f"bad --desc-window entry {chunk.strip()!r} — expected \"Name=YYYY-MM-DD:YYYY-MM-DD\""
            )
        for d in (start, end):
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"bad --desc-window date {d!r} — expected YYYY-MM-DD") from None
        if start > end:
            raise ValueError(f"--desc-window for {name!r}: start {start} is after end {end}")
        out[_normalize_name(name)] = (start, end)
    return out


def unknown_names(names, roster) -> list[str]:
    """A typo'd --manual name used to be silently ignored, leaving the person on a
    stale carried-over estimate. Return the unknown ones so the caller can refuse."""
    keys = {p["_key"] for p in roster}
    return [n for n in names if _normalize_name(n) not in keys]


def _us_date(iso: str) -> str:
    d = datetime.strptime(iso, "%Y-%m-%d").date()
    return f"{d.month}/{d.day}/{d.year}"


def build_descriptions(
    kimai: KimaiClient,
    begin: str,
    finish: str,
    desc_windows: dict[str, tuple[str, str]] | None = None,
    log: list[str] | None = None,
) -> tuple[dict, dict]:
    """Returns (descriptions, flags). Flags = {name: reason} for missing/repetitive
    Kimai descriptions (left empty + review note). Only fixed/kimai people who track
    in Kimai are checked. One batched Gemini call (quota-friendly).

    desc_windows overrides the Kimai window for individual people (one extra Kimai
    call each) — used when someone's default window holds nothing useful (e.g. PTO).
    """
    say = log.append if log is not None else print
    try:
        gem = GeminiSummarizer(load_gemini_key(), load_settings()["gemini"]["model"])
    except RuntimeError as e:
        say(f"  ! Descriptions skipped: {e}")
        return {}, {}

    by_user = kimai.descriptions_by_user(begin, finish)

    roster = load_roster()
    for key, (w_start, w_end) in (desc_windows or {}).items():
        person = next((p for p in roster if p["_key"] == key), None)
        if person is None or person.get("kimai_user_id") is None:
            say(f"  ! --desc-window ignored for {key!r}: not in roster.yaml or no kimai_user_id")
            continue
        uid = person["kimai_user_id"]
        custom = kimai.descriptions_by_user(
            f"{w_start}T00:00:00", f"{w_end}T23:59:59", user_id=uid
        )
        by_user[uid] = custom.get(uid) or []
        say(
            f"  Custom description window for {person['name']}: {w_start} -> {w_end} "
            f"({len(by_user[uid])} entries)"
        )

    flags: dict[str, str] = {}
    work: list[tuple[str, list[str]]] = []
    for p in roster:
        if not p.get("active", True):  # off the project -> no description needed
            continue
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
    # Anyone the summarizer could not write is flagged rather than filled with raw
    # Kimai text — those entries are internal notes and this cell goes to the client.
    for name, _entries in work:
        if name not in descriptions:
            flags[name] = "AI summary unavailable"
    if flags:
        say(f"  ! description flags (left empty + noted): {flags}")
    return descriptions, flags


def build_projects_summary(plan: InvoicePlan, log: list[str] | None = None) -> str | None:
    """One paragraph for the "PROJECTS ON THIS INVOICE" cell, condensed from every
    person's project cell as it will appear on the new tab (fresh AI description,
    else text carried over from the template tab). The previous invoice's paragraph
    is passed as the style/length reference so it stays the same size. One extra
    Gemini call; returns None on failure (the carried-over paragraph then stays)."""
    say = log.append if log is not None else print
    snap = plan.snapshot
    if snap is None:
        return None
    try:
        gem = GeminiSummarizer(load_gemini_key(), load_settings()["gemini"]["model"])
    except RuntimeError as e:
        say(f"  ! Projects summary skipped: {e}")
        return None

    # Include a person's blurb iff their row will actually be VISIBLE with text on
    # the new tab — mirroring _fill_new_tab's billed-row logic so the paragraph
    # matches the Project column exactly (no stale/hidden people, no omitted ones).
    blurbs: list[str] = []
    for li in plan.line_items:
        key = _normalize_name(li.name)
        row = snap.name_to_row.get(key)
        if row is None or key in plan.desc_flags:  # not on the sheet / cell blanked
            continue
        visible = True if li.hours is not None else row not in snap.hidden
        if not visible:
            continue
        desc = li.project_desc or snap.projects.get(row, "")
        if desc:
            blurbs.append(desc)

    return gem.summarize_projects(blurbs, snap.projects_paragraph)


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
    desc_window, no_kimai, no_descriptions, allow_duplicate_period."""
    g = lambda k, d=None: getattr(opts, k, d)  # noqa: E731
    settings = load_settings()
    roster = load_roster()

    manual_hours = parse_pairs(g("manual"), what="manual")
    passthroughs = parse_pairs(g("passthrough"), what="passthrough")
    desc_windows = parse_desc_windows(g("desc_window"))

    unknown = unknown_names(manual_hours, roster)
    if unknown:
        raise RuntimeError(
            "--manual names not in roster.yaml: "
            + ", ".join(repr(u) for u in unknown)
            + "\n(a typo here silently leaves the person on a stale carried-over estimate)"
        )
    unknown_win = [k for k in desc_windows if k not in {p["_key"] for p in roster}]
    if unknown_win:
        raise RuntimeError("--desc-window names not in roster.yaml: " + ", ".join(unknown_win))

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
                kimai, f"{d_start}T00:00:00", f"{d_end}T23:59:59", desc_windows, log
            )

    plan = build_plan(
        sheets,
        kimai_rows,
        start_date=_us_date(start),
        end_date=_us_date(end),
        invoice_date=_us_date(issue),
        cap=g("cap"),
        manual_hours=manual_hours,
        passthroughs=passthroughs,
        descriptions=descriptions,
        desc_flags=desc_flags,
        allow_duplicate_period=bool(g("allow_duplicate_period")),
    )
    if descriptions:  # fresh per-person cells exist -> refresh the overall paragraph too
        plan.projects_summary = build_projects_summary(plan, log)
    return InvoiceRun(plan, sheets, (start, end), issue, hours_window, desc_win, log)


def commit(run: InvoiceRun) -> str:
    """Write the prepared plan to a new sheet tab. Returns the tab name."""
    return write_plan(run.sheets, run.plan)
