"""EXECUTION — invoice builder/writer.

Computes the invoice plan (line items) and writes a new tab by duplicating the
latest invoice tab and filling only the variable cells (the sheet's own rate /
markup / discount formulas are preserved).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from execution.config import _normalize_name, load_roster, load_settings


@dataclass
class LineItem:
    name: str
    hours: float | None
    bill_rate: float
    source: str  # kimai | fixed | manual
    note: str = ""
    project_desc: str | None = None  # AI-generated description for column C

    @property
    def amount(self) -> float | None:
        if self.hours is None:
            return None
        return round(self.hours * self.bill_rate, 2)


@dataclass
class InvoicePlan:
    invoice_number: str
    template_tab: str
    start_date: str
    end_date: str
    invoice_date: str
    cap: float | None
    line_items: list[LineItem] = field(default_factory=list)
    passthroughs: dict[str, float] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    desc_flags: dict[str, str] = field(default_factory=dict)  # normalized name -> reason

    @property
    def subtotal(self) -> float:
        return round(
            sum(li.amount or 0 for li in self.line_items) + sum(self.passthroughs.values()), 2
        )

    @property
    def total(self) -> float:
        return self.cap if self.cap is not None else self.subtotal

    @property
    def discount(self) -> float:
        return round(self.subtotal - self.total, 2)


def latest_invoice_tab(titles: list[str], prefix: str) -> tuple[str, int]:
    pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    numbered = [(int(m.group(1)), t) for t in titles if (m := pat.match(t))]
    if not numbered:
        raise RuntimeError(f"No {prefix}-#### tabs found.")
    num, title = max(numbered)
    return title, num


def next_invoice_number(num: int, prefix: str, pad: int) -> str:
    return f"{prefix}-{num + 1:0{pad}d}"


def build_plan(
    sheets,
    kimai_rows: list[dict] | None,
    *,
    start_date: str,
    end_date: str,
    invoice_date: str,
    cap: float | None,
    manual_hours: dict[str, float] | None = None,
    passthroughs: dict[str, float] | None = None,
    descriptions: dict[str, str] | None = None,
    desc_flags: dict[str, str] | None = None,
) -> InvoicePlan:
    settings = load_settings()
    roster = load_roster()
    prefix = settings["invoice"]["prefix"]
    pad = settings["invoice"]["number_pad"]
    fixed_hours = settings["invoice"]["fixed_hours"]

    titles = sheets.sheet_titles()
    template_tab, latest_num = latest_invoice_tab(titles, prefix)
    inv_no = next_invoice_number(latest_num, prefix, pad)

    # If no cap given, carry over the PREVIOUS invoice's contract amount from its
    # discount cell (e.g. "=(SUM(F17:F36)-59250)" -> 59250). The reviewer can edit
    # the number in the formula bar; total defaults to the last agreed amount.
    if cap is None:
        try:
            dcell = settings["layout"]["discount_cell"]
            txt = str(sheets.get_values(f"'{template_tab}'!{dcell}", formulas=True)[0][0])
            m = re.search(r"-\s*([0-9][0-9.]*)\s*\)?\s*$", txt.replace(",", ""))
            if m:
                cap = float(m.group(1))
        except Exception:
            pass

    kimai_by_id: dict[int, float] = {}
    kimai_by_name: dict[str, float] = {}
    for row in kimai_rows or []:
        kimai_by_id[row["id"]] = row["hours"]
        for key in (row.get("username"), row.get("alias")):
            if key:
                kimai_by_name[_normalize_name(key)] = row["hours"]
    manual_idx = {_normalize_name(k): v for k, v in (manual_hours or {}).items()}
    desc_idx = {_normalize_name(k): v for k, v in (descriptions or {}).items()}

    plan = InvoicePlan(
        invoice_number=inv_no,
        template_tab=template_tab,
        start_date=start_date,
        end_date=end_date,
        invoice_date=invoice_date,
        cap=cap,
        passthroughs=passthroughs or {},
        desc_flags={_normalize_name(k): v for k, v in (desc_flags or {}).items()},
    )

    for p in roster:
        src = p["hours_source"]
        hours: float | None = None
        note = ""
        if src == "fixed":
            hours = fixed_hours
        elif src == "kimai":
            uid = p.get("kimai_user_id")
            alias = _normalize_name(p.get("kimai_alias") or "")
            if uid is not None and uid in kimai_by_id:
                hours = kimai_by_id[uid]
            elif alias and alias in kimai_by_name:
                hours = kimai_by_name[alias]
            elif uid is None and not alias:
                note = "no kimai_user_id / kimai_alias set in roster.yaml"
                plan.warnings.append(f"{p['name']}: {note}")
            else:
                hours = 0.0
                note = "no Kimai hours found for period"
                plan.warnings.append(f"{p['name']}: {note}")
        elif src == "manual":
            if p["_key"] in manual_idx:
                hours = manual_idx[p["_key"]]
            else:
                note = "estimate carried over from previous invoice (adjust later)"
                plan.warnings.append(f"{p['name']}: {note}")

        # A --manual override wins for ANY person, regardless of source.
        if p["_key"] in manual_idx:
            hours = manual_idx[p["_key"]]
            note = "manual override"
            plan.warnings = [w for w in plan.warnings if not w.startswith(p["name"] + ":")]

        plan.line_items.append(
            LineItem(
                name=p["name"],
                hours=hours,
                bill_rate=p["bill_rate"],
                source=src,
                note=note,
                project_desc=desc_idx.get(p["_key"]),
            )
        )

    return plan


def _col_row(col: str, row: int) -> str:
    return f"{col}{row}"


def write_plan(sheets, plan: InvoicePlan, tab_name: str | None = None) -> str:
    """Duplicates the template tab and fills the variable cells. Returns new tab name."""
    settings = load_settings()
    lay = settings["layout"]
    new_tab = tab_name or plan.invoice_number

    if new_tab in sheets.sheet_titles():
        raise RuntimeError(f"Tab {new_tab} already exists — aborting to avoid overwrite.")

    sheets.duplicate_sheet(plan.template_tab, new_tab)  # inserts right after the template tab

    r0, r1 = lay["line_items_scan_rows"]
    pcol, hcol, fcol = lay["person_col"], lay["hours_col"], lay["amount_col"]
    scan = sheets.get_values(f"'{new_tab}'!{pcol}{r0}:{pcol}{r1}")
    fforms = sheets.get_values(f"'{new_tab}'!{fcol}{r0}:{fcol}{r1}", formulas=True)
    name_to_row: dict[str, int] = {}
    f_formula: dict[int, str] = {}
    for i, row in enumerate(scan):
        if row and row[0]:
            name_to_row[_normalize_name(row[0])] = r0 + i
    for i, row in enumerate(fforms):
        if row and row[0] not in (None, ""):
            f_formula[r0 + i] = str(row[0])

    hidden = sheets.hidden_rows(new_tab, r0, r1)

    updates: list[tuple[str, object]] = []
    rows_set: set[int] = set()
    unhide: list[tuple[int, bool]] = []
    note_targets: list[tuple[int, str]] = []

    updates.append((lay["invoice_number"], plan.invoice_number))
    updates.append((lay["invoice_date"], plan.invoice_date))
    updates.append((lay["start_date"], plan.start_date))
    updates.append((lay["end_date"], plan.end_date))

    for li in plan.line_items:
        row = name_to_row.get(_normalize_name(li.name))
        if row is None:
            plan.warnings.append(f"{li.name}: row not found in {new_tab}, skipped")
            continue
        if li.hours is not None:
            updates.append((_col_row(hcol, row), li.hours))
            rows_set.add(row)
            if row in hidden:
                unhide.append((row, False))  # billing them -> show the row
        key = _normalize_name(li.name)
        if key in plan.desc_flags:
            updates.append((_col_row(lay["project_col"], row), ""))  # leave empty, flag below
            note_targets.append((row, plan.desc_flags[key]))
        elif li.project_desc:
            updates.append((_col_row(lay["project_col"], row), li.project_desc))

    for name, amount in plan.passthroughs.items():
        row = name_to_row.get(_normalize_name(name))
        if row is None:
            plan.warnings.append(f"passthrough '{name}': row not found, skipped")
            continue
        updates.append((_col_row(fcol, row), amount))
        rows_set.add(row)
        if row in hidden:
            unhide.append((row, False))

    # Option A: hidden rows we are NOT setting must not inflate the subtotal -> clear them.
    for row in hidden:
        if row in rows_set:
            continue
        formula = f_formula.get(row, "")
        if formula.startswith("=") and hcol.upper() in formula.upper():
            updates.append((_col_row(hcol, row), ""))   # people row -> blank the hours
        else:
            updates.append((_col_row(fcol, row), ""))   # pass-through -> blank the amount

    # Discount: cap -> discount = subtotal - cap; otherwise 0 (overwrites stale template cap).
    if plan.cap is not None:
        cap_str = f"{plan.cap:g}"  # 59250 (not 59250.0); keeps decimals if any
        updates.append((lay["discount_cell"], f"={lay['subtotal_cell']}-{cap_str}"))
    else:
        updates.append((lay["discount_cell"], 0))

    sheets.set_rows_hidden(new_tab, unhide)
    sheets.batch_update_cells(updates, new_tab)

    for row, reason in note_targets:
        sheets.set_cell_note(
            new_tab, row, lay["project_col"],
            f"⚠ Description {reason} for this period - please review & fill manually. (Areeba)",
        )
    return new_tab
