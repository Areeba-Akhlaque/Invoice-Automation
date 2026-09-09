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
from execution.gcal import CalendarClient
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
    kimai: KimaiClient | None,
    begin: str,
    finish: str,
    desc_windows: dict[str, tuple[str, str]] | None = None,
    calendar_entries: dict[str, list[str]] | None = None,
    log: list[str] | None = None,
) -> tuple[dict, dict]:
    """Returns (descriptions, flags). Flags = {name: reason} for a description we
    could not write (left empty + review note). One batched Gemini call.

    EVERY active person is considered, whatever their hours_source. People billed
    manually used to be skipped entirely, so their project cell simply carried the
    previous invoice's text forward -- James's and Bradd's lines were byte-identical
    across DRC-0061..0064. A person with no time-tracking source is now flagged
    rather than silently left stale.

    desc_windows overrides the window for individual people (one extra call each)
    -- used when someone's default window holds nothing useful (e.g. all PTO).
    """
    say = log.append if log is not None else print
    try:
        gem = GeminiSummarizer(load_gemini_key(), load_settings()["gemini"]["model"])
    except RuntimeError as e:
        say(f"  ! Descriptions skipped: {e}")
        return {}, {}

    by_user = kimai.descriptions_by_user(begin, finish) if kimai else {}

    roster = load_roster()
    for key, (w_start, w_end) in (desc_windows or {}).items():
        person = next((p for p in roster if p["_key"] == key), None)
        if person is None:
            continue
        if person["hours_source"] == "calendar":
            continue  # already pulled with its own window by prepare()
        if kimai is None or person.get("kimai_user_id") is None:
            say(f"  ! --desc-window ignored for {person['name']}: no Kimai source")
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
        if p["hours_source"] == "calendar":
            entries, source = (calendar_entries or {}).get(p["_key"]) or [], "calendar"
        elif p.get("kimai_user_id"):
            entries, source = by_user.get(p["kimai_user_id"]) or [], "Kimai"
        else:
            flags[p["name"]] = "no time-tracking source configured in roster.yaml"
            continue
        uniq = {" ".join(e.split()).strip().lower() for e in entries if e.strip()}
        if not uniq:
            flags[p["name"]] = f"no {source} entries in this period"
        elif len(entries) >= 2 and len(uniq) == 1:
            flags[p["name"]] = "repetitive (same entry all period)"
        else:
            work.append((p["name"], entries))

    descriptions = gem.summarize_batch(work)

    if work and not descriptions:
        # Nobody came back: the model is down or the daily quota is spent, not a
        # problem with anyone's time entries. Blanking every project cell over an
        # outage is worse than leaving last period's text in place, so write
        # nothing and let the carried-over cells stand — loudly.
        say(
            f"  !! AI descriptions UNAVAILABLE for all {len(work)} people (model error or "
            f"quota). Project cells keep the PREVIOUS invoice's text — check them before sending."
        )
        return {}, flags

    # An individual who came back empty is flagged rather than filled with raw
    # entries — those are internal notes and this cell goes to the client.
    for name, _entries in work:
        if name not in descriptions:
            flags[name] = "AI summary unavailable"
    if flags:
        say(f"  ! description flags (left empty + noted): {flags}")
    return descriptions, flags


def pull_calendars(
    credentials,
    settings: dict,
    roster: list[dict],
    hours_window: tuple[str, str],
    desc_windows: dict[str, tuple[str, str]],
    log: list[str],
) -> tuple[dict[str, float], dict[str, list[str]]]:
    """Hours and description material for everyone with hours_source: calendar.

    Their billable time is the events carrying their client's colour, matching the
    Apps Script that fills the calendar-sync sheet.

    The description covers exactly the events that produced the hours: same window,
    same colour. For Kimai people the two windows differ by design (hours are the
    previous complete half-month, descriptions the fortnight up to the issue date),
    but here the description IS the itemisation of the billed time, so they must
    agree. An explicit --desc-window still overrides it.
    """
    people = [p for p in roster if p["active"] and p["hours_source"] == "calendar"]
    if not people:
        return {}, {}

    cfg = settings.get("calendar") or {}
    client = CalendarClient(
        credentials,
        timezone=cfg.get("timezone", "UTC"),
        color_map=cfg.get("color_map"),
        skip_all_day=cfg.get("skip_all_day_events", True),
    )

    hours: dict[str, float] = {}
    entries: dict[str, list[str]] = {}
    for p in people:
        cal_id, project = p.get("calendar_id"), p.get("calendar_project")
        if not cal_id or not project:
            log.append(f"  ! {p['name']}: calendar_id / calendar_project missing in roster.yaml")
            continue
        try:
            pull = client.collect(cal_id, project, *hours_window)
        except Exception as e:  # noqa: BLE001 — a calendar we cannot read must not kill the run
            log.append(f"  ! {p['name']}: calendar read failed ({str(e)[:90]})")
            continue

        hours[p["_key"]] = pull.hours
        log.append(
            f"  Calendar {p['name']}: {pull.hours:g} h of '{project}' from "
            f"{pull.event_count} events ({hours_window[0]} -> {hours_window[1]})"
        )
        if pull.overlap_hours >= 0.5:
            log.append(
                f"  ! {p['name']}: {pull.overlap_hours:g} h counted twice from overlapping "
                f"events — the calendar-sync sheet does the same, but check it"
            )
        other = {k: v for k, v in pull.totals.items() if k != project and v >= 1}
        if other:
            log.append(f"    (other colours that period: {other})")

        d_window = desc_windows.get(p["_key"], hours_window)
        if d_window == hours_window:
            entries[p["_key"]] = pull.entries
        else:
            try:
                entries[p["_key"]] = client.collect(cal_id, project, *d_window).entries
                log.append(
                    f"  Calendar descriptions for {p['name']}: {d_window[0]} -> {d_window[1]} "
                    f"({len(entries[p['_key']])} events)"
                )
            except Exception as e:  # noqa: BLE001
                log.append(f"  ! {p['name']}: calendar description read failed ({str(e)[:80]})")
    return hours, entries


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

    creds = google_credentials(settings)
    sheets = SheetsClient(settings["google"]["spreadsheet_id"], creds)

    ph_start, ph_end = previous_half_month(start)
    hours_window = (g("hours_start") or ph_start, g("hours_end") or ph_end)
    desc_win = description_window(issue)

    kimai = None
    kimai_rows = None
    if not g("no_kimai"):
        env = load_kimai_env()
        kimai = KimaiClient(env["url"], env["token"], env["user"], env["verify_ssl"])
        kimai_rows = kimai.hours_with_identity(
            f"{hours_window[0]}T00:00:00", f"{hours_window[1]}T23:59:59"
        )
        log.append(
            f"Hours window (hourly estimate): {hours_window[0]} -> {hours_window[1]}  "
            f"({len(kimai_rows)} users)"
        )

    calendar_hours: dict[str, float] = {}
    calendar_entries: dict[str, list[str]] = {}
    if not g("no_calendar"):
        calendar_hours, calendar_entries = pull_calendars(
            creds, settings, roster, hours_window, desc_windows, log
        )

    descriptions: dict = {}
    desc_flags: dict = {}
    if not g("no_descriptions"):
        log.append(f"Descriptions window: {desc_win[0]} -> {desc_win[1]}")
        descriptions, desc_flags = build_descriptions(
            kimai,
            f"{desc_win[0]}T00:00:00",
            f"{desc_win[1]}T23:59:59",
            desc_windows,
            calendar_entries,
            log,
        )

    plan = build_plan(
        sheets,
        kimai_rows,
        start_date=_us_date(start),
        end_date=_us_date(end),
        invoice_date=_us_date(issue),
        cap=g("cap"),
        manual_hours=manual_hours,
        calendar_hours=calendar_hours,
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
