"""EXECUTION — Google Calendar adapter.

Some people bill from their calendar rather than Kimai: events are colour-coded
per client, and the client's colour is that person's billable time. This ports
the Apps Script that already produces those numbers in the calendar-sync sheet
(same colour map, same "end - start" duration, same all-day/cancelled skipping),
so the figures here match what that sheet reports.

Named gcal rather than calendar to avoid shadowing the stdlib module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from execution.retry import retry as _retry

DEFAULT_COLOR_LABEL = "No Color (default)"

# Boilerplate that meeting invites paste into the description; the Apps Script
# truncates at the first of these and so do we.
_BOILERPLATE = (
    "____",
    "Microsoft Teams Need Help",
    "Join the meeting",
    "Meeting ID",
    "Passcode",
    "Dial in by phone",
    "Find a local number",
    "More info",
    "Join on a video conferencing device",
    "Meeting options",
    "Confidentiality Notice",
)


def clean_description(description: str | None) -> str:
    if not description:
        return ""
    for kw in _BOILERPLATE:
        i = description.find(kw)
        if i != -1:
            description = description[:i]
    return " ".join(description.split()).strip()


@dataclass
class CalEvent:
    day: date
    start: datetime  # real local start/end, so overlaps can be measured
    end: datetime
    summary: str
    description: str
    minutes: float
    project: str

    @property
    def entry(self) -> str:
        """One line of source material for the AI description.

        Most events carry no description at all (3 of 87 in a sample fortnight),
        so the title has to do the work — and titles like "Echo1 Lead Sync" or
        "Data rules and integrity" are descriptive enough on their own.
        """
        return f"{self.summary} — {self.description}" if self.description else self.summary


def label_for(color_id, color_map: dict) -> str:
    """gCal colour id -> project label. Uncoloured events are their own bucket."""
    if color_id in (None, ""):
        return DEFAULT_COLOR_LABEL
    return color_map.get(str(color_id).strip(), "Unknown")


def parse_event(ev: dict, tz: ZoneInfo, color_map: dict, skip_all_day: bool) -> CalEvent | None:
    """One Calendar API item -> CalEvent, or None if it should not be billed."""
    if ev.get("status") == "cancelled":
        return None
    s, e = ev.get("start", {}), ev.get("end", {})
    if "dateTime" not in s:  # all-day: Home / trips / OOO, 1440 minutes of noise
        if skip_all_day:
            return None
        st = datetime.fromisoformat(s["date"]).replace(tzinfo=tz)
        en = datetime.fromisoformat(e["date"]).replace(tzinfo=tz)
    else:
        st = datetime.fromisoformat(s["dateTime"])
        en = datetime.fromisoformat(e["dateTime"])
    st_local, en_local = st.astimezone(tz), en.astimezone(tz)
    return CalEvent(
        day=st_local.date(),
        start=st_local,
        end=en_local,
        summary=(ev.get("summary") or "(no title)").strip(),
        description=clean_description(ev.get("description")),
        minutes=round((en - st) / timedelta(minutes=1), 2),
        project=label_for(ev.get("colorId"), color_map),
    )


class CalendarClient:
    def __init__(
        self,
        credentials,
        *,
        timezone: str = "UTC",
        color_map: dict | None = None,
        skip_all_day: bool = True,
    ):
        self.svc = build("calendar", "v3", credentials=credentials)
        self.tz = ZoneInfo(timezone)
        # YAML keys may be ints or strings; the API returns strings.
        self.color_map = {str(k): v for k, v in (color_map or {}).items()}
        self.skip_all_day = skip_all_day

    def events(self, calendar_id: str, start_iso: str, end_iso: str) -> list[CalEvent]:
        """All events in [start_iso, end_iso] (inclusive dates, local timezone)."""
        start = datetime.combine(date.fromisoformat(start_iso), time.min, tzinfo=self.tz)
        end = datetime.combine(date.fromisoformat(end_iso), time.max, tzinfo=self.tz)

        items: list[dict] = []
        page_token = None
        while True:
            resp = _retry(
                lambda token=page_token: self.svc.events()
                .list(
                    calendarId=calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=2500,
                    showDeleted=False,
                    pageToken=token,
                )
                .execute(),
                what=f"calendar {calendar_id} {start_iso}..{end_iso}",
            )
            items.extend(resp.get("items", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        out: list[CalEvent] = []
        for ev in items:
            parsed = parse_event(ev, self.tz, self.color_map, self.skip_all_day)
            if parsed is not None:
                out.append(parsed)
        return out

    # ---- what the invoice needs ----------------------------------------
    def collect(self, calendar_id: str, project: str, start_iso: str, end_iso: str) -> CalendarPull:
        """Hours and description material for one project label, in one API pass."""
        return summarise(self.events(calendar_id, start_iso, end_iso), project)


@dataclass
class CalendarPull:
    hours: float
    entries: list[str]
    event_count: int
    totals: dict[str, float]  # hours per colour label, for spotting mis-colouring
    overlap_hours: float  # time counted twice because two events overlapped


def summarise(events: list[CalEvent], project: str) -> CalendarPull:
    """Aggregate events for one project label.

    Durations are summed as-is, exactly like the Apps Script, so overlapping
    events are counted twice. That is reported separately rather than silently
    corrected, so the number still reconciles with the calendar-sync sheet
    (measured: 25 minutes across a sample fortnight, i.e. normally noise).
    """
    totals: dict[str, float] = {}
    for e in events:
        totals[e.project] = totals.get(e.project, 0) + e.minutes

    mine = [e for e in events if e.project == project]
    minutes = sum(e.minutes for e in mine)

    # A block titled after the project itself ("Ride Care") says nothing on a
    # Ride Care invoice, and there are many of them — drop them from the
    # description material (they still count toward the hours).
    label = " ".join(project.split()).lower()
    seen: set[str] = set()
    entries: list[str] = []
    for e in mine:
        if " ".join(e.summary.split()).lower() == label and not e.description:
            continue
        key = e.entry.lower()
        if key not in seen:
            seen.add(key)
            entries.append(e.entry)

    return CalendarPull(
        hours=round(minutes / 60.0, 2),
        entries=entries,
        event_count=len(mine),
        totals={k: round(v / 60.0, 2) for k, v in sorted(totals.items(), key=lambda x: -x[1])},
        overlap_hours=round((minutes - _wall_clock_minutes(mine)) / 60.0, 2),
    )


def _wall_clock_minutes(events: list[CalEvent]) -> float:
    """Elapsed minutes with overlapping events merged — the time actually spent."""
    spans = sorted((e.start, e.end) for e in events)
    if not spans:
        return 0.0
    total = 0.0
    cur_s, cur_e = spans[0]
    for s, e in spans:
        if s > cur_e:
            total += (cur_e - cur_s) / timedelta(minutes=1)
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    total += (cur_e - cur_s) / timedelta(minutes=1)
    return total
