"""EXECUTION — invoice builder/writer.

Computes the invoice plan (line items) and writes a new tab by duplicating the
latest invoice tab and filling only the variable cells (the sheet's own rate /
markup / discount formulas are preserved).

The sheet is the source of truth for rates and for any value we do not write, so
the plan reads a snapshot of the template tab first. That makes the preview equal
what the sheet will actually compute, and lets us fail loudly when the tab layout
has drifted instead of writing into the wrong cells.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from execution.config import _normalize_name, load_roster, load_settings


# --------------------------------------------------------------------------
# Template snapshot + layout validation
# --------------------------------------------------------------------------
@dataclass
class TemplateSnapshot:
    """What the tab we are about to duplicate already contains."""

    tab: str
    name_to_row: dict[str, int] = field(default_factory=dict)
    row_name: dict[int, str] = field(default_factory=dict)
    hours: dict[int, float | None] = field(default_factory=dict)
    rates: dict[int, float | None] = field(default_factory=dict)
    amounts: dict[int, float | None] = field(default_factory=dict)
    amount_formulas: dict[int, str] = field(default_factory=dict)
    projects: dict[int, str] = field(default_factory=dict)  # column C text already on the tab
    hidden: set[int] = field(default_factory=set)
    start_date: str = ""
    end_date: str = ""
    discount_formula: str = ""
    projects_paragraph: str = ""  # the "PROJECTS ON THIS INVOICE" cell


def _num(v) -> float | None:
    """UNFORMATTED_VALUE gives real numbers, but blanks/strings still show up."""
    if isinstance(v, int | float):
        return float(v)
    s = str(v or "").strip().replace("$", "").replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def read_template(sheets, tab: str, lay: dict) -> TemplateSnapshot:
    r0, r1 = lay["line_items_scan_rows"]
    # rates come back inside the hours..amount block read below, so rate_col
    # is not needed separately here.
    pcol, hcol, fcol = lay["person_col"], lay["hours_col"], lay["amount_col"]
    snap = TemplateSnapshot(tab=tab)

    names = sheets.get_values(f"'{tab}'!{pcol}{r0}:{pcol}{r1}")
    projects = sheets.get_values(f"'{tab}'!{lay['project_col']}{r0}:{lay['project_col']}{r1}")
    nums = sheets.get_values(f"'{tab}'!{hcol}{r0}:{fcol}{r1}", unformatted=True)
    forms = sheets.get_values(f"'{tab}'!{fcol}{r0}:{fcol}{r1}", formulas=True)

    for i in range(r1 - r0 + 1):
        row = r0 + i
        raw = names[i][0] if i < len(names) and names[i] else ""
        if str(raw).strip():
            snap.row_name[row] = str(raw)
            snap.name_to_row[_normalize_name(raw)] = row
        desc = projects[i][0] if i < len(projects) and projects[i] else ""
        if str(desc).strip():
            snap.projects[row] = " ".join(str(desc).split()).strip()
        cells = (nums[i] if i < len(nums) else []) + [None] * 3
        snap.hours[row] = _num(cells[0])
        snap.rates[row] = _num(cells[1])
        snap.amounts[row] = _num(cells[2])
        f = forms[i][0] if i < len(forms) and forms[i] else ""
        if str(f).strip():
            snap.amount_formulas[row] = str(f)

    snap.hidden = sheets.hidden_rows(tab, r0, r1)

    def one(cell: str, formulas: bool = False) -> str:
        v = sheets.get_values(f"'{tab}'!{cell}", formulas=formulas)
        return str(v[0][0]) if v and v[0] else ""

    snap.start_date = one(lay["start_date"])
    snap.end_date = one(lay["end_date"])
    snap.discount_formula = one(lay["discount_cell"], formulas=True)
    snap.projects_paragraph = one(lay["project_description"])
    return snap


def validate_layout(sheets, tab: str, lay: dict, prefix: str) -> list[str]:
    """Check settings.yaml's cell map still matches the real tab.

    The layout silently drifted by three rows once already (the code wrote the
    discount into an empty cell below the table for months). Returns a list of
    problems; the caller aborts if it is non-empty.
    """
    problems: list[str] = []
    r0, r1 = lay["line_items_scan_rows"]
    fcol = lay["amount_col"]

    def formula_of(cell: str) -> str:
        try:
            v = sheets.get_values(f"'{tab}'!{cell}", formulas=True)
        except Exception as e:  # noqa: BLE001 — an out-of-grid cell 400s
            return f"<unreadable: {str(e)[:60]}>"
        return str(v[0][0]) if v and v[0] else ""

    sub = formula_of(lay["subtotal_cell"])
    m = re.search(rf"SUM\(\s*{fcol}(\d+)\s*:\s*{fcol}(\d+)\s*\)", sub, re.I)
    if not m:
        problems.append(
            f"subtotal_cell {lay['subtotal_cell']} does not contain a "
            f"=SUM({fcol}..:{fcol}..) formula (found: {sub!r})"
        )
    elif (int(m.group(1)), int(m.group(2))) != (r0, r1):
        problems.append(
            f"line_items_scan_rows {[r0, r1]} != the subtotal's SUM range "
            f"{[int(m.group(1)), int(m.group(2))]} in {lay['subtotal_cell']}"
        )

    disc = formula_of(lay["discount_cell"])
    if lay["subtotal_cell"].upper() not in disc.upper():
        problems.append(
            f"discount_cell {lay['discount_cell']} does not reference "
            f"{lay['subtotal_cell']} (found: {disc!r})"
        )

    tot = formula_of(lay["total_cell"])
    if lay["subtotal_cell"].upper() not in tot.upper():
        problems.append(
            f"total_cell {lay['total_cell']} does not reference "
            f"{lay['subtotal_cell']} (found: {tot!r})"
        )

    try:
        inv = sheets.get_values(f"'{tab}'!{lay['invoice_number']}")
        inv_txt = str(inv[0][0]) if inv and inv[0] else ""
    except Exception:  # noqa: BLE001
        inv_txt = ""
    if not re.match(rf"^{re.escape(prefix)}-\d+$", inv_txt.strip()):
        problems.append(
            f"invoice_number cell {lay['invoice_number']} holds {inv_txt!r}, "
            f"not a {prefix}-#### value"
        )
    return problems


# --------------------------------------------------------------------------
# Plan
# --------------------------------------------------------------------------
@dataclass
class LineItem:
    name: str
    hours: float | None  # hours we will WRITE (None -> leave the carried value)
    bill_rate: float  # from roster.yaml (preview only)
    source: str  # kimai | fixed | manual
    note: str = ""
    project_desc: str | None = None  # AI-generated description for column C
    carried_hours: float | None = None  # what the template tab already has
    sheet_rate: float | None = None  # column E on the sheet — the real billing rate

    @property
    def effective_hours(self) -> float | None:
        """What the sheet will show: our value if we write one, else the carried one."""
        return self.hours if self.hours is not None else self.carried_hours

    @property
    def effective_rate(self) -> float:
        """The sheet's rate wins — its formulas, not roster.yaml, produce the invoice."""
        return self.sheet_rate if self.sheet_rate is not None else self.bill_rate

    @property
    def amount(self) -> float | None:
        h = self.effective_hours
        return None if h is None else round(h * self.effective_rate, 2)


@dataclass
class InvoicePlan:
    invoice_number: str
    template_tab: str
    start_date: str
    end_date: str
    invoice_date: str
    cap: float | None
    line_items: list[LineItem] = field(default_factory=list)
    passthroughs: dict[str, float] = field(default_factory=dict)  # CLI overrides
    carried_passthroughs: dict[str, float] = field(default_factory=dict)  # already on the tab
    warnings: list[str] = field(default_factory=list)
    desc_flags: dict[str, str] = field(default_factory=dict)  # normalized name -> reason
    projects_summary: str | None = None  # "PROJECTS ON THIS INVOICE" paragraph
    retired: list[str] = field(default_factory=list)  # active:false -> clear their row
    snapshot: TemplateSnapshot | None = None

    @property
    def effective_passthroughs(self) -> dict[str, float]:
        """CLI override wins; otherwise whatever the duplicated tab already carries."""
        out = dict(self.carried_passthroughs)
        for name, amt in self.passthroughs.items():
            out[name] = amt
        return out

    @property
    def subtotal(self) -> float:
        return round(
            sum(li.amount or 0 for li in self.line_items)
            + sum(self.effective_passthroughs.values()),
            2,
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


def parse_carried_cap(discount_formula: str, subtotal_cell: str) -> float | None:
    """Read the agreed contract amount out of the previous discount formula.

    Accepts only the two shapes we actually write/expect —
    '=F34-51188' and '=(SUM(F17:F32)-51188)'. A bare number, a chained
    '=F34-51188-500', or a cell reference returns None rather than a wrong cap:
    a misread here silently rewrites the invoice total.
    """
    txt = str(discount_formula or "").replace(",", "").strip()
    pat = rf"^=\s*\(?\s*(?:SUM\([^)]*\)|{re.escape(subtotal_cell)})\s*-\s*([0-9]+(?:\.[0-9]+)?)\s*\)?\s*$"
    m = re.match(pat, txt, re.I)
    return float(m.group(1)) if m else None


def build_plan(
    sheets,
    kimai_rows: list[dict] | None,
    *,
    start_date: str,
    end_date: str,
    invoice_date: str,
    cap: float | None,
    manual_hours: dict[str, float] | None = None,
    calendar_hours: dict[str, float] | None = None,
    passthroughs: dict[str, float] | None = None,
    descriptions: dict[str, str] | None = None,
    desc_flags: dict[str, str] | None = None,
    allow_duplicate_period: bool = False,
) -> InvoicePlan:
    settings = load_settings()
    roster = load_roster()
    lay = settings["layout"]
    prefix = settings["invoice"]["prefix"]
    pad = settings["invoice"]["number_pad"]
    fixed_hours = settings["invoice"]["fixed_hours"]

    titles = sheets.sheet_titles()
    template_tab, latest_num = latest_invoice_tab(titles, prefix)
    inv_no = next_invoice_number(latest_num, prefix, pad)

    problems = validate_layout(sheets, template_tab, lay, prefix)
    if problems:
        raise RuntimeError(
            f"Invoice tab layout does not match directive/settings.yaml (checked '{template_tab}'):\n"
            + "\n".join(f"  - {p}" for p in problems)
            + "\nFix the `layout:` block in directive/settings.yaml before running."
        )

    snap = read_template(sheets, template_tab, lay)

    # Guard: running twice for the same period would silently create a second
    # invoice for work already billed on the template tab.
    if snap.start_date.strip() == start_date and snap.end_date.strip() == end_date:
        msg = (
            f"'{template_tab}' is already an invoice for {start_date} -> {end_date}. "
            f"Creating {inv_no} would bill the same period twice."
        )
        if not allow_duplicate_period:
            raise RuntimeError(msg + "\nUse --allow-duplicate-period if this is intentional.")

    if cap is None:
        cap = parse_carried_cap(snap.discount_formula, lay["subtotal_cell"])

    kimai_by_id: dict[int, float] = {}
    kimai_by_name: dict[str, float] = {}
    for row in kimai_rows or []:
        kimai_by_id[row["id"]] = row["hours"]
        for key in (row.get("username"), row.get("alias")):
            if key:
                kimai_by_name[_normalize_name(key)] = row["hours"]
    manual_idx = {_normalize_name(k): v for k, v in (manual_hours or {}).items()}
    calendar_idx = calendar_hours or {}
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
        snapshot=snap,
    )
    if snap.start_date.strip() == start_date and snap.end_date.strip() == end_date:
        plan.warnings.append(
            f"same period as '{template_tab}' — proceeding because --allow-duplicate-period was given"
        )

    roster_keys = {p["_key"] for p in roster}

    for p in roster:
        if not p.get("active", True):  # off the project -> no line item, row gets cleared
            plan.retired.append(p["name"])
            continue
        src = p["hours_source"]
        row = snap.name_to_row.get(p["_key"])
        hours: float | None = None
        note = ""

        if row is None:
            # Used to be appended during write_plan, after the preview had already
            # been printed — so a name that stopped matching dropped a person from
            # the invoice with no visible sign at all.
            note = f"NOT FOUND in column {lay['person_col']} of '{template_tab}' — will not be billed"
            plan.warnings.append(f"{p['name']}: {note}")

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
        elif src == "calendar":
            if p["_key"] in calendar_idx:
                hours = calendar_idx[p["_key"]]
            else:
                note = "no calendar hours for this period (calendar not read?)"
                plan.warnings.append(f"{p['name']}: {note}")
        elif src == "manual" and p["_key"] not in manual_idx:
            note = "estimate carried over from previous invoice (adjust later)"
            plan.warnings.append(f"{p['name']}: {note}")

        # A --manual override wins for ANY person, regardless of source.
        if p["_key"] in manual_idx:
            hours = manual_idx[p["_key"]]
            note = "manual override"
            plan.warnings = [w for w in plan.warnings if not w.startswith(p["name"] + ":")]

        sheet_rate = snap.rates.get(row) if row is not None else None
        if sheet_rate is not None and abs(sheet_rate - p["bill_rate"]) > 0.005:
            plan.warnings.append(
                f"{p['name']}: rate mismatch — sheet ${sheet_rate:,.2f}, "
                f"roster.yaml ${p['bill_rate']:,.2f} (the sheet bills; update roster.yaml)"
            )

        plan.line_items.append(
            LineItem(
                name=p["name"],
                hours=hours,
                bill_rate=p["bill_rate"],
                source=src,
                note=note,
                project_desc=desc_idx.get(p["_key"]),
                carried_hours=snap.hours.get(row) if row is not None else None,
                sheet_rate=sheet_rate,
            )
        )

    # Everything else in the billable range that is not a roster person is a
    # pass-through row; it keeps its carried amount unless --passthrough overrides it.
    override_keys = {_normalize_name(k) for k in plan.passthroughs}
    for row, raw in snap.row_name.items():
        key = _normalize_name(raw)
        if key in roster_keys or key in override_keys or row in snap.hidden:
            continue
        amt = snap.amounts.get(row)
        if amt:
            plan.carried_passthroughs[raw] = amt

    for name in plan.passthroughs:
        if _normalize_name(name) not in snap.name_to_row:
            plan.warnings.append(
                f"passthrough '{name}': no row with that name in '{template_tab}' — will not be written"
            )

    return plan


def _col_row(col: str, row: int) -> str:
    return f"{col}{row}"


def write_plan(sheets, plan: InvoicePlan, tab_name: str | None = None) -> str:
    """Duplicates the template tab and fills the variable cells. Returns new tab name.

    If anything fails after the duplicate is created, the half-written tab is
    deleted — otherwise it would sit in the sheet holding the *previous*
    invoice's numbers and also block the retry (the name would already exist).
    """
    settings = load_settings()
    lay = settings["layout"]
    new_tab = tab_name or plan.invoice_number

    if new_tab in sheets.sheet_titles(refresh=True):
        raise RuntimeError(f"Tab {new_tab} already exists — aborting to avoid overwrite.")

    sheets.duplicate_sheet(plan.template_tab, new_tab)  # inserts right after the template tab
    try:
        return _fill_new_tab(sheets, plan, new_tab, lay)
    except Exception:
        try:
            sheets.delete_sheet(new_tab)
            print(f"  ! write failed — rolled back (deleted the partial tab '{new_tab}')")
        except Exception as cleanup_err:  # noqa: BLE001
            print(
                f"  !! write failed AND rollback failed: '{new_tab}' is half-written "
                f"and must be deleted by hand ({str(cleanup_err)[:80]})"
            )
        raise


def _fill_new_tab(sheets, plan: InvoicePlan, new_tab: str, lay: dict) -> str:
    r0, r1 = lay["line_items_scan_rows"]
    pcol, hcol, fcol = lay["person_col"], lay["hours_col"], lay["amount_col"]

    # The duplicate is identical to the template, so reuse the snapshot we already read.
    snap = plan.snapshot or read_template(sheets, new_tab, lay)
    name_to_row = snap.name_to_row
    f_formula = snap.amount_formulas
    hidden = snap.hidden

    updates: list[tuple[str, object]] = []
    rows_set: set[int] = set()
    row_vis: list[tuple[int, bool]] = []  # (row, hidden?) — show billed, hide off-boarded
    note_targets: list[tuple[int, str]] = []

    updates.append((lay["invoice_number"], plan.invoice_number))
    updates.append((lay["invoice_date"], plan.invoice_date))
    updates.append((lay["start_date"], plan.start_date))
    updates.append((lay["end_date"], plan.end_date))
    if plan.projects_summary:
        updates.append((lay["project_description"], plan.projects_summary))

    for li in plan.line_items:
        row = name_to_row.get(_normalize_name(li.name))
        if row is None:
            continue  # already warned in build_plan, before the preview was printed
        if li.hours is not None:
            updates.append((_col_row(hcol, row), li.hours))
            rows_set.add(row)
            if row in hidden:
                row_vis.append((row, False))  # billing them -> show the row
        key = _normalize_name(li.name)
        if key in plan.desc_flags:
            updates.append((_col_row(lay["project_col"], row), ""))  # leave empty, flag below
            note_targets.append((row, plan.desc_flags[key]))
        elif li.project_desc:
            updates.append((_col_row(lay["project_col"], row), li.project_desc))

    for name, amount in plan.passthroughs.items():
        row = name_to_row.get(_normalize_name(name))
        if row is None:
            continue  # already warned in build_plan
        updates.append((_col_row(fcol, row), amount))
        rows_set.add(row)
        if row in hidden:
            row_vis.append((row, False))

    # Off the project (roster active:false): the duplicated tab still carries their
    # name + hours from the previous invoice, so clear the whole line (name, project,
    # hours) and LEAVE THE ROW VISIBLE (not hidden). Blanking the name also means the
    # next invoice's scan won't find them, so they stay gone going forward. Must run
    # before Option A (which only touches hidden rows anyway).
    for name in plan.retired:
        row = name_to_row.get(_normalize_name(name))
        if row is None:
            continue
        updates.append((_col_row(pcol, row), ""))  # name
        updates.append((_col_row(lay["project_col"], row), ""))  # project text
        updates.append((_col_row(hcol, row), ""))  # hours -> amount goes blank
        rows_set.add(row)  # keep Option A from touching it too

    # Option A: hidden rows we are NOT setting must not inflate the subtotal -> clear them.
    # A person row is one whose AMOUNT formula references the hours column (D17, not
    # merely the letter "D" — "ROUND(" contains one and used to match).
    for row in hidden:
        if row in rows_set:
            continue
        formula = f_formula.get(row, "")
        if formula.startswith("=") and re.search(rf"\b{hcol}\d+\b", formula, re.I):
            updates.append((_col_row(hcol, row), ""))  # people row -> blank the hours
        else:
            updates.append((_col_row(fcol, row), ""))  # pass-through -> blank the amount

    # Discount: cap -> discount = subtotal - cap; otherwise 0 (overwrites stale template cap).
    if plan.cap is not None:
        cap_str = f"{plan.cap:g}"  # 59250 (not 59250.0); keeps decimals if any
        updates.append((lay["discount_cell"], f"={lay['subtotal_cell']}-{cap_str}"))
    else:
        updates.append((lay["discount_cell"], 0))

    sheets.set_rows_hidden(new_tab, row_vis)
    sheets.batch_update_cells(updates, new_tab)

    for row, reason in note_targets:
        sheets.set_cell_note(
            new_tab,
            row,
            lay["project_col"],
            f"⚠ Description {reason} for this period - please review & fill manually. (Areeba)",
        )
    return new_tab
