# Directive layer

The **what** — declarative configuration and billing policy. No logic here; the
Orchestration layer reads these files and the Execution layer enforces them.

| File | Holds |
|---|---|
| `settings.yaml` | spreadsheet id, invoice rules, schedule (run days), Gemini model, sheet cell layout, pass-through rows |
| `roster.yaml` | each person: rate, markup, `hours_source` (fixed/kimai/manual), `kimai_user_id`, `active` |

## The billing directives (Cherry's model)
- **Advance billing:** invoice for period `[S,E]` is issued `S+7` (1-15 → 8th, 16-30 → 23rd); due = `E`. The draft is generated on the **5th & 20th** (midnight Pacific), a few days ahead of the issue date so there is time to review. Moving those days does not move the issue date — that is computed from the period.
- **Full-timers** → `86.5` hrs (estimate). **Hourly** → Kimai actuals from the **previous complete half-month**. **Calendar-billed** → the client's colour on their Google Calendar, same window. **Manual** → carried over from the previous invoice (estimate, adjusted later).
- **Descriptions** → AI-summarized from each person's time entries in `[issue-15, issue-1]`, whatever their source. Missing/repetitive/ungeneratable → left empty + a review note. Raw Kimai or calendar text is **never** copied to the invoice; those are internal notes.
- **Total** = calculated subtotal. The discount cell carries the previously agreed contract amount forward as an editable formula; the reviewer (Cherry) adjusts it.
- **Hidden rows** with leftover values are cleared so the subtotal always equals the visible line items.

## Two things that must stay in sync
1. **`name` must match column B of the invoice tab exactly** (whitespace/case
   ignored). A name that stops matching means the person is not billed — the run
   now warns, but the fix belongs here. *(This is how "Prameeth" vs the sheet's
   "Prameeth Kotian" went unnoticed and carried stale hours forward.)*
2. **The `layout:` block must match the real tab.** `validate_layout()` checks it
   on every run and aborts on a mismatch, because the cells silently drifted three
   rows once (subtotal `F34`, discount `F35`, total `D37` — not `F37/F38/D40`).

Rates in `roster.yaml` are **preview only** — the sheet's column E does the billing.
The run warns when the two disagree.

## `hours_source: calendar`
For people who bill from their Google Calendar rather than a timesheet (James has
no Kimai entries at all). Their billable time is the events carrying their client's
colour; the same events' titles feed the AI description.

```yaml
    hours_source: calendar
    calendar_id: "someone@example.com"
    calendar_project: "Ride Care"     # a label from settings.yaml calendar.color_map
```

Same colour map and all-day/cancelled handling as the Apps Script behind the
calendar-sync sheet, and verified against it event-for-event. **No cap is applied
— every hour on the calendar is billable, however long the day runs.**

**A working day runs 4am to 4am** (`calendar.day_start_hour`). James works past
midnight and that evening's block is that evening's work, so the period 1-15 Oct
means 1 Oct 04:00 through 16 Oct 04:00. On a midnight boundary a block finishing
at 1:30am had 1.5 h pushed into the next invoice.

- **Double-booked time is billed twice** (`calendar.count_overlapping_time`), on
  the client's instruction: meetings overrun their slots and the calendar's
  marked times understate that. The amount is reported every run as
  `overlap_hours` — 0.25 to 2.33 h a fortnight — so a figure counted the other
  way can always be reconciled.
- **Events are clipped to the billed window.** The Calendar API returns anything
  overlapping it, so a block that began before the period would otherwise bill
  its earlier hours to this invoice.

Also worth watching: **uncoloured time is billed to nobody** — it has run at
2-53 h per half-month. `python check_setup.py` reports it every run.

## Kimai project filter
This invoice bills one client's work, but people log time against more than one
project (Ride Care and Pvragon, 2110 h and 835 h over Sep-Oct). `kimai.project`
in `settings.yaml` names the one being invoiced, so another client's hours and
descriptions cannot land on it.

A person whose billable work sits under a different project overrides it:

```yaml
    kimai_projects: ["Pvragon", "Ride Care"]   # Dana: the SW1045 bid work
```

Their time is pulled separately with their own filter, and a name that is not a
Kimai project stops the run rather than quietly billing nothing.

## `carry_description: true`
For someone with no time source to summarise from, whose project line does not
really change — Bradd, until his calendar is coloured. The run writes nothing to
their project cell, so the duplicated tab keeps the text it already has, and the
cell is not flagged. Without this flag a person with no source has their cell
emptied and noted every invoice, which is the right default: better an obvious
gap than months-old text passing as current.

## `active: false`
Someone off the project — permanently (off-boarded) or temporarily (no hours for
now). They get no line item, and if their row still exists on the copied tab it is
cleared and left visible. Keep the entry rather than deleting it: deleting would
leave the inherited hours quietly billing. Flip back to `true` when they return.

To change behaviour, edit these files — not the code.
