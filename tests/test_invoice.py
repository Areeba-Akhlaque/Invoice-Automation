"""Regression tests for the invoice builder.

Every case here corresponds to a real defect found on the live sheet in Sept 2026
(see the audit in the project history) — a wrong number in any of these silently
changes what a client is billed.
"""
import re

import pytest

from execution.invoice import (
    InvoicePlan,
    LineItem,
    latest_invoice_tab,
    next_invoice_number,
    parse_carried_cap,
    validate_layout,
)

SUBTOTAL = "F34"


# --------------------------------------------------------------------------
# Contract amount carried over from the previous invoice's discount formula
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "formula,expected",
    [
        ("=F34-51188", 51188.0),
        ("=(SUM(F17:F32)-51188)", 51188.0),
        ("=F34 - 55500", 55500.0),
        ("=F34-51188.50", 51188.50),
        ("=F34-51,188", 51188.0),
    ],
)
def test_parse_carried_cap_accepts_the_shapes_we_write(formula, expected):
    assert parse_carried_cap(formula, SUBTOTAL) == expected


@pytest.mark.parametrize(
    "formula",
    [
        "-2500",             # a hand-typed flat discount: NOT a contract amount
        "2500",
        "0",
        "",
        "=F34-F35",          # reference, not a number
        "=F34-51188-500",    # chained: which one is the cap? neither — refuse
        "=SUM(F17:F32)-F41",
        "=OTHER-51188",      # not our subtotal cell
    ],
)
def test_parse_carried_cap_refuses_anything_ambiguous(formula):
    """A misread here rewrites the invoice total, so silence beats a guess."""
    assert parse_carried_cap(formula, SUBTOTAL) is None


# --------------------------------------------------------------------------
# Tab numbering
# --------------------------------------------------------------------------
def test_latest_invoice_tab_picks_the_highest_number():
    titles = ["Summary", "DRC-0009", "DRC-0064", "DRC-0065", "DRC-0054 Detailed Log"]
    assert latest_invoice_tab(titles, "DRC") == ("DRC-0065", 65)


def test_latest_invoice_tab_raises_when_none_present():
    with pytest.raises(RuntimeError, match="No DRC"):
        latest_invoice_tab(["Summary", "Notes"], "DRC")


def test_next_invoice_number_pads():
    assert next_invoice_number(65, "DRC", 4) == "DRC-0066"
    assert next_invoice_number(9, "DRC", 4) == "DRC-0010"


# --------------------------------------------------------------------------
# Line items: what the SHEET will show, not just what we write
# --------------------------------------------------------------------------
def test_effective_hours_falls_back_to_the_carried_value():
    """Manual people get no written hours — the duplicated row keeps the previous
    invoice's value and is still billed, so the preview must include it."""
    li = LineItem(name="James Hereford", hours=None, bill_rate=200, source="manual",
                  carried_hours=120.0, sheet_rate=200.0)
    assert li.effective_hours == 120.0
    assert li.amount == 24000.0


def test_written_hours_win_over_carried():
    li = LineItem(name="Prameeth Kotian", hours=69.83, bill_rate=30, source="kimai",
                  carried_hours=86.5, sheet_rate=30.0)
    assert li.effective_hours == 69.83
    assert li.amount == pytest.approx(2094.90)


def test_sheet_rate_wins_over_roster_rate():
    """roster.yaml drifted from the sheet three times; the sheet's formula bills."""
    li = LineItem(name="JP Casabianca", hours=86.5, bill_rate=48.75, source="fixed",
                  sheet_rate=58.80)
    assert li.effective_rate == 58.80
    assert li.amount == pytest.approx(5086.20)


def test_amount_is_none_when_there_are_no_hours_at_all():
    li = LineItem(name="Nobody", hours=None, bill_rate=10, source="manual")
    assert li.effective_hours is None
    assert li.amount is None


# --------------------------------------------------------------------------
# Subtotal must equal what the sheet's =SUM() will produce
# --------------------------------------------------------------------------
def _plan(**kw) -> InvoicePlan:
    base = dict(invoice_number="DRC-0066", template_tab="DRC-0065", start_date="9/1/2026",
                end_date="9/15/2026", invoice_date="9/8/2026", cap=None)
    base.update(kw)
    return InvoicePlan(**base)


def test_subtotal_includes_carried_hours_and_carried_passthroughs():
    plan = _plan(
        line_items=[
            LineItem("Written", 10, 100, "fixed", sheet_rate=100.0),
            LineItem("Carried", None, 200, "manual", carried_hours=2.0, sheet_rate=200.0),
        ],
        carried_passthroughs={"Google Cloud Platform": 520.0},
    )
    assert plan.subtotal == 1000 + 400 + 520


def test_cli_passthrough_overrides_the_carried_amount_and_is_not_double_counted():
    plan = _plan(
        passthroughs={"Google Cloud Platform": 999.0},
        carried_passthroughs={"Softstackers AWS Infrastructure": 2270.21},
    )
    assert plan.effective_passthroughs == {
        "Softstackers AWS Infrastructure": 2270.21,
        "Google Cloud Platform": 999.0,
    }
    assert plan.subtotal == pytest.approx(3269.21)


def test_discount_and_total_follow_the_cap():
    plan = _plan(cap=51188.0, carried_passthroughs={"x": 63542.51})
    assert plan.total == 51188.0
    assert plan.discount == pytest.approx(12354.51)


def test_no_cap_means_total_equals_subtotal_and_zero_discount():
    plan = _plan(carried_passthroughs={"x": 100.0})
    assert plan.total == 100.0
    assert plan.discount == 0


# --------------------------------------------------------------------------
# Option A: telling a person row from a pass-through row by its AMOUNT formula
# --------------------------------------------------------------------------
def _is_person_row(formula: str, hours_col: str = "D") -> bool:
    """Mirrors the check in _fill_new_tab."""
    return formula.startswith("=") and bool(re.search(rf"\b{hours_col}\d+\b", formula, re.I))


@pytest.mark.parametrize(
    "formula", ['=if(isblank(D17),"",D17*E17)', "=D22*E22", "=ROUND(D19*E19,2)"]
)
def test_person_rows_are_detected(formula):
    assert _is_person_row(formula)


@pytest.mark.parametrize(
    "formula", ["=((3783.69/2)*1.2)", "=ROUND(1642.46,2)", "=520", "=SUM(F20:F22)"]
)
def test_passthrough_rows_are_not_mistaken_for_person_rows(formula):
    """`"D" in "=ROUND(...)"` used to be True — ROUND contains a D — which blanked
    the wrong column and left a stale pass-through amount in the subtotal."""
    assert not _is_person_row(formula)


# --------------------------------------------------------------------------
# Layout validation against a fake sheet
# --------------------------------------------------------------------------
LAYOUT = {
    "invoice_number": "F5", "invoice_date": "F6", "start_date": "F8", "end_date": "F9",
    "project_description": "B13", "person_col": "B", "project_col": "C", "hours_col": "D",
    "rate_col": "E", "amount_col": "F", "line_items_scan_rows": [17, 32],
    "subtotal_cell": "F34", "discount_cell": "F35", "total_cell": "D37",
}


class FakeSheets:
    def __init__(self, cells: dict[str, str]):
        self.cells = cells

    def get_values(self, a1_range, formulas=False, unformatted=False):
        cell = a1_range.split("!")[-1]
        v = self.cells.get(cell)
        return [[v]] if v is not None else []


def _good_sheet(**overrides):
    cells = {
        "F34": "=SUM(F17:F32)",
        "F35": "=F34-51188",
        "D37": "=F34-F35",
        "F5": "DRC-0065",
    }
    cells.update(overrides)
    return FakeSheets(cells)


def test_validate_layout_passes_on_a_correct_tab():
    assert validate_layout(_good_sheet(), "DRC-0065", LAYOUT, "DRC") == []


def test_validate_layout_catches_the_three_row_drift():
    """The real failure: settings.yaml pointed at F37/F38/D40 while the tab used
    F34/F35/D37, so the discount was written into an empty cell for months."""
    drifted = dict(LAYOUT, subtotal_cell="F37", discount_cell="F38", total_cell="D40")
    problems = validate_layout(_good_sheet(), "DRC-0065", drifted, "DRC")
    assert problems
    assert any("subtotal_cell F37" in p for p in problems)


def test_validate_layout_catches_scan_rows_out_of_sync_with_the_sum():
    wrong = dict(LAYOUT, line_items_scan_rows=[16, 36])
    problems = validate_layout(_good_sheet(), "DRC-0065", wrong, "DRC")
    assert any("SUM range" in p for p in problems)


def test_validate_layout_catches_a_discount_that_lost_its_subtotal_reference():
    problems = validate_layout(_good_sheet(F35="-2500"), "DRC-0065", LAYOUT, "DRC")
    assert any("discount_cell" in p for p in problems)


def test_validate_layout_catches_a_non_invoice_tab():
    problems = validate_layout(_good_sheet(F5="Summary"), "DRC-0065", LAYOUT, "DRC")
    assert any("invoice_number" in p for p in problems)
