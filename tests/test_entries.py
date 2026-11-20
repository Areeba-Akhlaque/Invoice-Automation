"""Stripping recurring meetings out of time-entry text.

Every case here is a real entry from the 1-15 Nov 2026 period. The complaint that
prompted this was a project cell reading "Dev Team All Hands, Pvragon Coworking,
1:1 meetings, ..." — meeting names that are identical every fortnight, in a cell
that is supposed to tell the client what was built.

Two things have to hold, and the second is the one worth protecting: meetings must
go, and real work must survive. Wrongly dropping work is the expensive mistake,
because nobody notices a deliverable that quietly stopped being billed for.
"""
import pytest

from execution.config import load_roster, load_settings
from execution.entries import (
    clean_entries,
    clean_entry,
    compile_patterns,
    is_meeting_noise,
    people_names,
    scrub_meeting_mentions,
    strip_boilerplate,
    unsupported_terms,
)

# The live list, so a change to settings.yaml that breaks these is caught here and
# not on a client invoice.
PATTERNS = compile_patterns((load_settings().get("descriptions") or {}).get("drop_segments"))


def noise(text: str) -> bool:
    return is_meeting_noise(text, PATTERNS)


# --------------------------------------------------------------------------
# Meeting-only entries: these must go
# --------------------------------------------------------------------------
MEETINGS = [
    "Dev Team All Hands",
    "Pvragon Coworking",
    "Pvragon Team Building",
    "Pvragon Team Building 🎮",
    "Billing Team Sync",
    "Billing Team Weekly",
    "Support Review",
    "Echo1 Office Hours",
    "Echo 1 office hours",
    "Echo1 Office hour",
    "Echo1 Data Team Sync",
    "Ride Care Ops + Data Leads",
    "Strategy - Pvragon",
    "Driver Trip Log Review and Planning",
    "Crisis session",
    "Crisis Changes session",
    "Crisis Classification call",
    "Review",
    "1:1",
    "1:1 meetings",
    "1:1 with Bradd",
    "Jaime TB",
    "Victor : Jaime Opportunities & Career",
]


@pytest.mark.parametrize("entry", MEETINGS)
def test_a_recurring_meeting_is_dropped(entry):
    assert noise(entry), entry


# Name-pair meetings, recognised by shape rather than listed: a denylist would
# need a new line every time two people start a 1:1.
PAIRS = [
    "Roman : Jaime",
    "Roman : jaime",
    "Roman : Jaime 1:1",
    "Victor / Roman",
    "Maranda / Roman",
    "Victor / Bradd",
    "Victor - Bradd",
    "Vic - Bradd 1:1",
    "Bradd / James",
    "James / Bradd",
    "JP / James",
    "JP : Jaime Weekly",
    "Macie : Jaime 1:1",
    "Trever : Jaime",
    "Dana/Jaime biweekly",
    "David :James Bi Weekly Meeting",
    "Toni + Comms",
    "Vic and William + Bradd",
]


@pytest.mark.parametrize("entry", PAIRS)
def test_a_one_to_one_is_dropped_by_its_shape(entry):
    assert noise(entry), entry


TIME_OFF = ["PAID OFF DAY - Veterans Day", "Company Holiday", "Compnay Holiday", "PTO"]


@pytest.mark.parametrize("entry", TIME_OFF)
def test_time_off_is_not_described_as_work(entry):
    assert noise(entry), entry


# --------------------------------------------------------------------------
# Real work: these must survive
# --------------------------------------------------------------------------
WORK = [
    # Meetings that name a subject are the subject, and belong on the invoice.
    "Aetna and Humana breakouts",
    "Humana/RideCare claims discussion",
    "IMPORTANT REVIEW: Crisis Classification Rollout",
    "JP:Bradd Crisis Review",
    "RideCare Brand Review",
    # Ordinary work entries.
    "Merged sign-to-clear PR #3405",
    "Scoped one-time bulk sign of 132 trip logs",
    "Reviewed crisis scheduling ticket against crisis classification work",
    "Refined the spec for the Transport \"Duplicate As...\" modal",
    "TIPS and Harris law enforcemnet bids",
    "bidscout sweep and tweaks/strategy",
    "looking for more opportunities, weeding through the shortlist",
    "#284, TIPS, Canada follow ups, #37",
    "testing session with William",
    "ops(billing): rides split across claims by rate where that recovers more",
    "Worked in Testing strategy, Shadow tenant, Infra audit tickets.",
]


@pytest.mark.parametrize("entry", WORK)
def test_real_work_is_kept(entry):
    assert not noise(entry), entry


def test_a_subject_bearing_review_is_not_confused_with_the_recurring_one():
    """"Review" alone is the standing meeting Saymond logs; the same word with a
    subject attached is work. Dropping both would delete real deliverables."""
    assert noise("Review")
    assert not noise("RideCare Brand Review")
    assert not noise("Reviewed Phase 2 trip-log signatures against Phase 1 and closed it")


# --------------------------------------------------------------------------
# Mixed entries: meetings prefix the work, and the tail must survive
# --------------------------------------------------------------------------
def test_a_meeting_prefix_is_stripped_and_the_work_kept():
    """Saymond's real shape: the meetings he attended, then what he actually did.
    Dropping the whole entry would have lost the work; keeping it whole put the
    meeting names on the invoice."""
    entry = (
        "Echo1 Office Hours; Dev Team All Hands; Pvragon Coworking. - Echo1 QA "
        "pipeline: finished the draft diagram and the supporting doc."
    )
    out = clean_entry(entry, PATTERNS)
    assert out.startswith("Echo1 QA pipeline")
    for gone in ("Office Hours", "All Hands", "Coworking"):
        assert gone not in out


def test_a_mixed_entry_whose_work_mentions_a_meeting_keeps_that_context():
    entry = (
        "Dev Team All Hands; 1:1 with Bradd. - Missed-request cleanup handed over "
        "to Trever off the back of the Support Review."
    )
    out = clean_entry(entry, PATTERNS)
    assert "Missed-request cleanup" in out
    assert "All Hands" not in out
    assert "1:1" not in out
    # "Support Review" survives inside a sentence about real work -- the pattern
    # matches a whole segment, not a substring, on purpose.
    assert "Support Review" in out


def test_an_entry_of_nothing_but_meetings_comes_back_empty():
    """Which is what makes the caller flag the person and leave a cell note,
    instead of describing them with a plausible list of meeting names."""
    assert clean_entry("Dev Team All Hands; Pvragon Coworking", PATTERNS) == ""
    assert clean_entry("Roman : Jaime 1:1", PATTERNS) == ""


# --------------------------------------------------------------------------
# Pasted invitation boilerplate
# --------------------------------------------------------------------------
def test_zoom_boilerplate_is_cut_from_a_calendar_title():
    entry = (
        "Humana/RideCare claims discussion — Let me know if this time doesn't "
        "work. [https://zoom.us/account/branding/p/4398e8dd-0000]"
    )
    assert strip_boilerplate(entry) == "Humana/RideCare claims discussion"


def test_boilerplate_is_cut_at_the_earliest_marker_not_the_first_listed():
    entry = "Crisis Review Meeting ID 123 ____ Join the meeting"
    assert strip_boilerplate(entry) == "Crisis Review"


def test_an_entry_with_no_boilerplate_is_untouched():
    entry = "Merged sign-to-clear PR #3405"
    assert strip_boilerplate(entry) == entry


# --------------------------------------------------------------------------
# The batch helper
# --------------------------------------------------------------------------
def test_clean_entries_reports_how_many_it_dropped():
    """The count goes in the run log, so material shrinking to nothing is visible
    rather than only showing up as a flag."""
    kept, dropped = clean_entries(
        ["Dev Team All Hands", "Merged sign-to-clear PR #3405", "Victor / Roman"],
        PATTERNS,
    )
    assert kept == ["Merged sign-to-clear PR #3405"]
    assert dropped == 2


def test_clean_entries_on_an_empty_list():
    assert clean_entries([], PATTERNS) == ([], 0)


def test_a_bad_pattern_in_settings_is_refused_rather_than_ignored():
    """Silently skipping an unparseable pattern would stop the filtering without
    anyone noticing."""
    with pytest.raises(RuntimeError, match="not a valid regular expression"):
        compile_patterns(["(unclosed"])


# --------------------------------------------------------------------------
# The configured list must actually match the live data it was written for
# --------------------------------------------------------------------------
def test_the_configured_patterns_are_all_in_use():
    """A pattern that matches nothing in MEETINGS is either a typo or dead config.
    This caught 'echo1 office hour' (singular) being missed."""
    unmatched = [
        p.pattern
        for p in PATTERNS
        if not any(noise(m) and p.fullmatch(_norm(m)) for m in MEETINGS + TIME_OFF)
    ]
    assert not unmatched, f"patterns matching nothing in this period's data: {unmatched}"


def _norm(text: str) -> str:
    s = " ".join(text.split()).lstrip("-•*").strip()
    return s.rstrip(".,;:").strip().lower()


# --------------------------------------------------------------------------
# Stripping colleagues' names, keeping the subject
# --------------------------------------------------------------------------
NAMES = people_names(load_roster(), (load_settings().get("descriptions") or {}).get("people"))
SCRUB = compile_patterns((load_settings().get("descriptions") or {}).get("scrub_phrases"))


def test_a_meeting_about_something_keeps_the_something():
    """"JP:Bradd Crisis Review" is billable work about the crisis review; who sat
    in it is not. The pair is unspaced, which is how it survived the first pass."""
    assert clean_entry("JP:Bradd Crisis Review", PATTERNS, NAMES) == "Crisis Review"


def test_a_pair_with_no_subject_is_still_dropped():
    assert clean_entry("Roman : Jaime 1:1", PATTERNS, NAMES) == ""
    assert clean_entry("Victor : Jaime Opportunities & Career", PATTERNS, NAMES) == ""


@pytest.mark.parametrize("entry", [
    "Humana/RideCare claims discussion",
    "TIPS and Harris law enforcemnet bids",
    "Aetna and Humana breakouts",
    "IMPORTANT REVIEW: Crisis Classification Rollout",
    "Merged sign-to-clear PR #3405",
    "bidscout sweep and tweaks/strategy",
    "#284, TIPS, Canada follow ups, #37",
])
def test_name_stripping_leaves_real_work_alone(entry):
    """Clients and payers are named like people but ARE the work. Stripping them
    would quietly delete what is being billed for."""
    assert clean_entry(entry, PATTERNS, NAMES) == entry


# --------------------------------------------------------------------------
# Scrubbing the finished description
# --------------------------------------------------------------------------
def test_a_meeting_named_inside_prose_is_cut_from_the_finished_line():
    """Segment filtering cannot reach these: "Daily support call and team
    meetings. Ticket scoping..." is one segment, so the model keeps writing them
    into the cell. His support calls are the work; the team meetings are not."""
    out = scrub_meeting_mentions(
        "Daily support calls and team meetings, ticket scoping and triage, "
        "Chatwoot conversation updates, Echo1 Office Hours, and test record cleanup.",
        SCRUB,
    )
    assert "Daily support calls" in out
    assert "team meetings" not in out
    assert "Office Hours" not in out
    assert "test record cleanup" in out


def test_scrubbing_removes_the_company_prefix_with_the_label():
    """Cutting only "coworking" left a bare "Pvragon" reading as a deliverable."""
    out = scrub_meeting_mentions(
        "Dev Team All Hands, Pvragon Coworking, 1:1 meetings, billing QA.", SCRUB
    )
    assert "Pvragon" not in out
    # Capitalised, because removing the first three items promoted it to the front.
    assert out == "Billing QA."


def test_scrubbing_recapitalises_when_it_removes_the_first_item():
    out = scrub_meeting_mentions("Dev Team All Hands, billing QA, and crisis changes.", SCRUB)
    assert out[0].isupper(), out


def test_a_line_with_no_meetings_is_returned_unchanged():
    """Identity matters: a needless rewrite risks mangling a good description."""
    good = "Route sign-off fixes, failed dispatch handling, and audit logging fixes."
    assert scrub_meeting_mentions(good, SCRUB) == good


def test_a_line_that_was_nothing_but_meetings_comes_back_empty():
    """Which makes the caller flag the person rather than bill a cell of meetings."""
    assert scrub_meeting_mentions("Dev Team All Hands, Pvragon Coworking, 1:1 meetings.", SCRUB) == ""


# --------------------------------------------------------------------------
# Work the model invented
# --------------------------------------------------------------------------
PABLO = [
    "Worked in Testing strategy, Shadow tenant, Infra audit tickets. Made several Prs.",
    "Worked in Staging Prod tenant. Merged two PRs and tested in AWS. Updated open PRs.",
    "Worked in Staging Prod tenant. Merged two PRs and had a call to prepare dry run.",
]


def test_an_invented_system_name_is_caught():
    """A real case: ten entries about infra audit tickets were summarised as
    citing "Storybook test updates", which appears in none of them. This cell
    goes to a client, so a named system with no time entry behind it has to be
    stopped on rather than billed."""
    desc = (
        "Staging Prod tenant updates, PR merges, and AWS testing, including infra "
        "audit ticket PRs, Storybook test updates, and Shadow tenant ticket PRs."
    )
    assert unsupported_terms(desc, PABLO) == ["Storybook"]


def test_the_accurate_version_of_the_same_summary_is_clean():
    """The guard has to be quiet on the good output, or it gets ignored."""
    desc = (
        "Staging Prod tenant work, infra audit ticket PRs, testing strategy PRs, "
        "Shadow tenant ticket PRs, AWS testing, and PR updates."
    )
    assert unsupported_terms(desc, PABLO) == []


def test_payers_and_systems_that_are_really_in_the_entries_pass():
    entries = [
        "Aetna and Humana breakouts",
        "IMPORTANT REVIEW: Crisis Classification Rollout",
        "Humana/RideCare claims discussion",
        "RideCare Brand Review",
    ]
    desc = (
        "Crisis Classification Rollout review, Humana/RideCare claims discussion, "
        "Aetna and Humana breakouts, and RideCare Brand Review."
    )
    assert unsupported_terms(desc, entries) == []


def test_ordinary_capitalised_words_are_not_reported():
    """Paraphrasing is the model's job. Flagging every capitalised word would
    bury the one case that matters."""
    assert unsupported_terms("Billing resubmissions and coverage checks.", ["ops(billing): resubmissions, coverage"]) == []
    assert unsupported_terms("Daily support calls and ticket triage.", ["support call, triage"]) == []


def test_an_acronym_the_entries_never_mention_is_reported():
    assert unsupported_terms("Audit logging and HIPAA tracking.", ["audit logging work"]) == ["HIPAA"]


def test_a_term_is_reported_once_however_often_it_appears():
    desc = "Storybook tests, Storybook snapshots, and Storybook coverage."
    assert unsupported_terms(desc, PABLO) == ["Storybook"]


def test_nothing_to_check_against_does_not_crash():
    assert unsupported_terms("", []) == []
    assert unsupported_terms("Storybook work.", []) == ["Storybook"]
