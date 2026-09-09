"""End-to-end test of the tab writer against a fake Google Sheet.

write_plan touches a live client invoice, so its behaviour is pinned here rather
than discovered in production: which cells get written, which rows get cleared,
and that a failure mid-write rolls the half-built tab back.
"""
import pytest

from execution.invoice import InvoicePlan, LineItem, TemplateSnapshot, write_plan

LAY = {
    "invoice_number": "F5", "invoice_date": "F6", "start_date": "F8", "end_date": "F9",
    "project_description": "B13", "person_col": "B", "project_col": "C", "hours_col": "D",
    "rate_col": "E", "amount_col": "F", "line_items_scan_rows": [17, 32],
    "subtotal_cell": "F34", "discount_cell": "F35", "total_cell": "D37",
}


class FakeSheets:
    """Records what write_plan does instead of calling Google."""

    def __init__(self, titles, fail_on_write=False):
        self.titles = list(titles)
        self.fail_on_write = fail_on_write
        self.written: dict[str, object] = {}
        self.duplicated: tuple[str, str] | None = None
        self.deleted: list[str] = []
        self.visibility: list[tuple[int, bool]] = []
        self.notes: list[tuple[int, str, str]] = []

    def sheet_titles(self, refresh=False):
        return self.titles

    def duplicate_sheet(self, src, new, insert_index=None):
        self.duplicated = (src, new)
        self.titles.append(new)

    def delete_sheet(self, title):
        self.deleted.append(title)
        self.titles.remove(title)

    def batch_update_cells(self, updates, tab):
        if self.fail_on_write:
            raise ConnectionError("connection reset by peer")
        for cell, val in updates:
            self.written[cell] = val

    def set_rows_hidden(self, tab, changes):
        self.visibility.extend(changes)

    def set_cell_note(self, tab, row, col, note):
        self.notes.append((row, col, note))


@pytest.fixture
def snapshot():
    """Mirrors the real DRC layout: people 17-26, pass-throughs 31-32."""
    snap = TemplateSnapshot(tab="DRC-0065")
    rows = {
        17: "James Hereford", 18: "Roman Naidenko", 19: "Victor Cheung",
        20: "Saymond Montoya", 25: "Prameeth Kotian", 26: "Bradd Schofield",
        30: "Clarissa", 31: "Softstackers AWS Infrastructure", 32: "Google Cloud Platform",
    }
    for row, name in rows.items():
        snap.row_name[row] = name
        snap.name_to_row[name.lower()] = row
    snap.amount_formulas = {
        17: '=if(isblank(D17),"",D17*E17)', 25: "=D25*E25", 30: "=D30*E30",
        31: "=((3783.69/2)*1.2)", 32: "=ROUND(520,2)",
    }
    return snap


def _plan(snapshot, **kw) -> InvoicePlan:
    base = dict(
        invoice_number="DRC-0066", template_tab="DRC-0065", start_date="9/1/2026",
        end_date="9/15/2026", invoice_date="9/8/2026", cap=51188.0, snapshot=snapshot,
    )
    base.update(kw)
    return InvoicePlan(**base)


def test_writes_header_hours_and_discount_formula(snapshot):
    sheets = FakeSheets(["DRC-0064", "DRC-0065"])
    plan = _plan(snapshot, line_items=[
        LineItem("Roman Naidenko", 86.5, 58, "fixed", sheet_rate=58.0),
        LineItem("Prameeth Kotian", 69.83, 30, "kimai", sheet_rate=30.0,
                 project_desc="Ticket triage, SMS testing."),
    ])
    assert write_plan(sheets, plan) == "DRC-0066"

    assert sheets.duplicated == ("DRC-0065", "DRC-0066")
    assert sheets.written["F5"] == "DRC-0066"
    assert sheets.written["F8"] == "9/1/2026"
    assert sheets.written["F9"] == "9/15/2026"
    assert sheets.written["D18"] == 86.5
    assert sheets.written["D25"] == 69.83
    assert sheets.written["C25"] == "Ticket triage, SMS testing."
    # The discount is written as a live formula so the reviewer can edit the amount.
    assert sheets.written["F35"] == "=F34-51188"


def test_manual_person_with_no_override_keeps_the_carried_hours(snapshot):
    """We must NOT write a value — the row keeps the previous invoice's estimate."""
    sheets = FakeSheets(["DRC-0065"])
    plan = _plan(snapshot, line_items=[
        LineItem("James Hereford", None, 200, "manual", carried_hours=120.0, sheet_rate=200.0),
    ])
    write_plan(sheets, plan)
    assert "D17" not in sheets.written


def test_no_cap_zeroes_the_discount_instead_of_leaving_a_stale_one(snapshot):
    sheets = FakeSheets(["DRC-0065"])
    write_plan(sheets, _plan(snapshot, cap=None))
    assert sheets.written["F35"] == 0


def test_retired_person_row_is_cleared_and_left_visible(snapshot):
    sheets = FakeSheets(["DRC-0065"])
    write_plan(sheets, _plan(snapshot, retired=["Clarissa"]))
    assert sheets.written["B30"] == ""  # name
    assert sheets.written["C30"] == ""  # project text
    assert sheets.written["D30"] == ""  # hours -> amount goes blank
    assert (30, True) not in sheets.visibility  # not hidden, just emptied


def test_missing_person_is_skipped_without_crashing(snapshot):
    sheets = FakeSheets(["DRC-0065"])
    plan = _plan(snapshot, line_items=[LineItem("Dana Hette", 10, 144, "kimai")])
    write_plan(sheets, plan)
    assert not any(c.startswith("D") and c != "D37" for c in sheets.written)


def test_flagged_description_blanks_the_cell_and_leaves_a_note(snapshot):
    sheets = FakeSheets(["DRC-0065"])
    plan = _plan(snapshot, desc_flags={"victor cheung": "AI summary unavailable"},
                 line_items=[LineItem("Victor Cheung", 86.5, 52, "fixed", sheet_rate=52.0)])
    write_plan(sheets, plan)
    assert sheets.written["C19"] == ""
    assert sheets.notes and sheets.notes[0][0] == 19
    assert "AI summary unavailable" in sheets.notes[0][2]


def test_hidden_person_row_has_its_hours_cleared(snapshot):
    """Otherwise a hidden leftover row silently inflates the subtotal."""
    snapshot.hidden = {17}
    sheets = FakeSheets(["DRC-0065"])
    write_plan(sheets, _plan(snapshot))
    assert sheets.written["D17"] == ""


def test_hidden_passthrough_row_has_its_amount_cleared_not_its_hours(snapshot):
    """'=ROUND(520,2)' contains a D — it must still be treated as a pass-through."""
    snapshot.hidden = {32}
    sheets = FakeSheets(["DRC-0065"])
    write_plan(sheets, _plan(snapshot))
    assert sheets.written["F32"] == ""
    assert "D32" not in sheets.written


def test_billing_a_hidden_row_unhides_it(snapshot):
    snapshot.hidden = {25}
    sheets = FakeSheets(["DRC-0065"])
    plan = _plan(snapshot, line_items=[LineItem("Prameeth Kotian", 69.83, 30, "kimai")])
    write_plan(sheets, plan)
    assert (25, False) in sheets.visibility


def test_refuses_to_overwrite_an_existing_tab(snapshot):
    sheets = FakeSheets(["DRC-0065", "DRC-0066"])
    with pytest.raises(RuntimeError, match="already exists"):
        write_plan(sheets, _plan(snapshot))
    assert sheets.duplicated is None


def test_a_failed_write_rolls_the_partial_tab_back(snapshot):
    """A half-written tab holds the PREVIOUS invoice's numbers and would also block
    the retry, because its name already exists."""
    sheets = FakeSheets(["DRC-0065"], fail_on_write=True)
    with pytest.raises(ConnectionError):
        write_plan(sheets, _plan(snapshot))
    assert sheets.deleted == ["DRC-0066"]
    assert "DRC-0066" not in sheets.titles
