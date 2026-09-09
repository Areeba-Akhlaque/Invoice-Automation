"""CLI option parsing + roster integrity.

A typo in --manual used to be swallowed silently, leaving that person on a stale
carried-over estimate; and a roster name that stops matching column B drops the
person from the invoice entirely.
"""
import pytest

from execution.config import _normalize_name, load_roster, load_settings
from orchestration.orchestrator import parse_desc_windows, parse_pairs, unknown_names


# --------------------------------------------------------------------------
# parse_pairs
# --------------------------------------------------------------------------
def test_parse_pairs_reads_names_and_numbers():
    assert parse_pairs("James Hereford=116,Bradd Schofield=50.5") == {
        "James Hereford": 116.0,
        "Bradd Schofield": 50.5,
    }


def test_parse_pairs_tolerates_blank_and_whitespace():
    assert parse_pairs("  A = 1 , ,B=2 ") == {"A": 1.0, "B": 2.0}
    assert parse_pairs(None) == {}
    assert parse_pairs("") == {}


@pytest.mark.parametrize("bad", ["A", "A=", "=5", "A=abc"])
def test_parse_pairs_rejects_malformed_entries_with_a_readable_error(bad):
    with pytest.raises(ValueError, match="manual"):
        parse_pairs(bad, what="manual")


# --------------------------------------------------------------------------
# parse_desc_windows
# --------------------------------------------------------------------------
def test_parse_desc_windows():
    assert parse_desc_windows("Victor Cheung=2026-08-10:2026-08-24") == {
        "victor cheung": ("2026-08-10", "2026-08-24")
    }


def test_parse_desc_windows_handles_several_people():
    got = parse_desc_windows("A=2026-01-01:2026-01-15,B=2026-02-01:2026-02-10")
    assert got == {"a": ("2026-01-01", "2026-01-15"), "b": ("2026-02-01", "2026-02-10")}


@pytest.mark.parametrize(
    "bad",
    [
        "Victor Cheung",                      # no window
        "Victor Cheung=2026-08-10",           # no end
        "Victor Cheung=10/08/2026:24/08/2026",  # wrong date format
        "Victor Cheung=2026-08-24:2026-08-10",  # backwards
    ],
)
def test_parse_desc_windows_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        parse_desc_windows(bad)


# --------------------------------------------------------------------------
# Name validation
# --------------------------------------------------------------------------
def test_unknown_names_flags_typos():
    roster = [{"_key": "james hereford"}, {"_key": "bradd schofield"}]
    assert unknown_names(["Jame Hereford"], roster) == ["Jame Hereford"]
    assert unknown_names(["  james   HEREFORD "], roster) == []


# --------------------------------------------------------------------------
# Directive-layer integrity (catches the class of bug that hid Prameeth)
# --------------------------------------------------------------------------
def test_roster_entries_are_well_formed():
    for p in load_roster():
        assert p["name"].strip(), "a roster entry has an empty name"
        assert p["hours_source"] in {"fixed", "kimai", "manual", "calendar"}, p["name"]
        assert isinstance(p["base_rate"], (int, float)), p["name"]
        assert p["bill_rate"] > 0, p["name"]


def test_roster_names_are_unique_after_normalisation():
    keys = [p["_key"] for p in load_roster()]
    assert len(keys) == len(set(keys)), "two roster entries normalise to the same name"


def test_calendar_sourced_people_have_a_calendar_and_project():
    for p in load_roster():
        if p["active"] and p["hours_source"] == "calendar":
            assert p.get("calendar_id"), f"{p['name']} has no calendar_id"
            assert p.get("calendar_project"), f"{p['name']} has no calendar_project"


def test_calendar_projects_exist_in_the_colour_map():
    labels = set(load_settings()["calendar"]["color_map"].values())
    for p in load_roster():
        if p["active"] and p["hours_source"] == "calendar":
            assert p["calendar_project"] in labels, (
                f"{p['name']}: calendar_project {p['calendar_project']!r} is not in "
                f"settings.yaml calendar.color_map"
            )


def test_kimai_sourced_people_have_a_user_id():
    missing = [
        p["name"]
        for p in load_roster()
        if p["active"] and p["hours_source"] == "kimai" and not p.get("kimai_user_id")
    ]
    assert not missing, f"kimai_user_id missing for: {missing}"


def test_layout_scan_rows_are_sane():
    lay = load_settings()["layout"]
    r0, r1 = lay["line_items_scan_rows"]
    assert r0 < r1
    subtotal_row = int(lay["subtotal_cell"][1:])
    assert r1 < subtotal_row, "the scan range must stop above the subtotal row"


def test_normalize_name_is_whitespace_and_case_insensitive():
    assert _normalize_name("  Roman   Naidenko  ") == _normalize_name("roman naidenko")


# --------------------------------------------------------------------------
# Behaviour when the summarizer itself is down
# --------------------------------------------------------------------------
class _DeadSummarizer:
    """Model unreachable / daily quota spent: returns nothing for everyone."""

    unavailable = True

    def summarize_batch(self, people):
        return {}


class _PartialSummarizer:
    """Answers for the first person only, with the API otherwise healthy."""

    unavailable = False

    def summarize_batch(self, people):
        return {people[0][0]: "Some real work, and more work."} if people else {}


class _QuotaRanOutSummarizer:
    """The real shape of a spent free-tier quota: some answers, then 429s."""

    unavailable = True

    def summarize_batch(self, people):
        return {people[0][0]: "Some real work, and more work."} if people else {}


def _run_build_descriptions(monkeypatch, summarizer, entries):
    import orchestration.orchestrator as orch

    monkeypatch.setattr(orch, "load_gemini_key", lambda: "key")
    monkeypatch.setattr(orch, "GeminiSummarizer", lambda *a, **k: summarizer)
    monkeypatch.setattr(orch, "load_roster", lambda: [
        {"name": "A", "_key": "a", "active": True, "hours_source": "calendar"},
        {"name": "B", "_key": "b", "active": True, "hours_source": "calendar"},
    ])
    return orch.build_descriptions(None, "x", "y", {}, entries, [])


def test_a_total_summarizer_outage_leaves_the_carried_text_alone(monkeypatch):
    """Blanking every project cell because the API was down is worse than keeping
    last period's text — the run says so loudly instead."""
    descriptions, flags = _run_build_descriptions(
        monkeypatch, _DeadSummarizer(), {"a": ["Echo1 Lead Sync"], "b": ["Data Migration"]}
    )
    assert descriptions == {}
    assert flags == {}  # nothing flagged -> write_plan writes nothing -> cells carry over


def test_a_partial_quota_failure_does_not_blank_the_rest(monkeypatch):
    """Quota ran out mid-run: one person answered, seven were about to have their
    project cells emptied over an outage that says nothing about their work."""
    descriptions, flags = _run_build_descriptions(
        monkeypatch, _QuotaRanOutSummarizer(),
        {"a": ["Echo1 Lead Sync"], "b": ["Data Migration"]},
    )
    assert "A" in descriptions
    assert flags == {}  # B is not flagged -> B's cell keeps its carried text


def test_one_person_missing_is_still_flagged(monkeypatch):
    """A single failure is about that person's entries, so their cell is emptied
    and noted rather than left showing someone else's stale work."""
    descriptions, flags = _run_build_descriptions(
        monkeypatch, _PartialSummarizer(), {"a": ["Echo1 Lead Sync"], "b": ["Data Migration"]}
    )
    assert "A" in descriptions
    assert flags == {"B": "AI summary unavailable"}


def test_people_with_no_entries_are_flagged_not_left_stale(monkeypatch):
    """James's and Bradd's cells were identical across four invoices."""
    _descriptions, flags = _run_build_descriptions(
        monkeypatch, _PartialSummarizer(), {"a": ["Echo1 Lead Sync"], "b": []}
    )
    assert flags == {"B": "no calendar entries in this period"}


# --------------------------------------------------------------------------
# Period options: the workflow exposes start and end as separate free-text inputs
# --------------------------------------------------------------------------
class _Opts:
    def __init__(self, **kw):
        self.__dict__.update(
            dict(start=None, end=None, invoice_date=None, hours_start=None, hours_end=None,
                 cap=None, manual=None, passthrough=None, desc_window=None, no_kimai=True,
                 no_calendar=True, no_descriptions=True, allow_duplicate_period=False), **kw
        )


def _prepare_fails(**kw) -> str:
    import pytest as _pytest

    from orchestration.orchestrator import prepare

    with _pytest.raises((RuntimeError, ValueError)) as e:
        prepare(_Opts(**kw))
    return str(e.value)


def test_start_without_end_is_refused():
    """Filling only one used to discard BOTH and silently invoice the automatic
    period instead — dates nobody asked for."""
    assert "together" in _prepare_fails(start="2026-10-01")


def test_end_without_start_is_refused():
    assert "together" in _prepare_fails(end="2026-10-15")


def test_a_malformed_period_date_is_refused():
    assert "YYYY-MM-DD" in _prepare_fails(start="01/10/2026", end="2026-10-15")


def test_a_backwards_period_is_refused():
    assert "after" in _prepare_fails(start="2026-10-15", end="2026-10-01")


def test_a_malformed_hours_window_is_refused():
    assert "YYYY-MM-DD" in _prepare_fails(hours_start="not-a-date")


def test_a_backwards_hours_window_is_refused():
    assert "after" in _prepare_fails(hours_start="2026-10-15", hours_end="2026-10-01")


def test_shell_metacharacters_in_a_date_are_just_an_invalid_date():
    """Workflow inputs reach argv, never a shell — this must read as a bad date."""
    assert "YYYY-MM-DD" in _prepare_fails(start="2026-10-01; rm -rf /", end="2026-10-15")
