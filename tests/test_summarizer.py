"""The description length budget.

The project cell is one line in a narrow column on a client-facing invoice, so its
length is a layout constraint. Gemini overshoots it badly for anyone with a long
entry list — a 25-ticket fortnight came back as 771 characters, seven times the
house length — so the budget is stated in the prompt AND enforced afterwards. These
tests cover the enforcement, which is the part that does not depend on the model
behaving.
"""
import json

from execution import summarizer
from execution.summarizer import (
    _DESC_MAX_CHARS,
    _DESC_MAX_WORDS,
    GeminiSummarizer,
    _too_long,
    _trim_to_phrase,
)

# 31 words, but 436 characters: inside the word cap and far over the character
# cap. This is why there are two bounds — a line of long compound nouns passes a
# word count and still overflows the cell.
LONG_WORDS = (
    "Classification-evidence grid implementation, audit-trail reconciliation across "
    "downloads and exports, documentation remediation planning, infrastructure "
    "decommissioning verification, reimbursement reconciliation investigation, "
    "authorization-requirement clarification, notification-recipient reconfiguration, "
    "transportation-dashboard consolidation, facility-address discrepancy remediation, "
    "and background-task readiness instrumentation."
)

# 23 short items, 57 words, ~490 characters: over both bounds. The real shape that
# reached the sheet when nothing was enforced at all.
LONG = (
    "Trip-log corrections, test-data automation, audit trail improvements, database "
    "logging, old system shutdown, ride log signing, route optimizer investigation, "
    "test restore, shutdown reports, employee management fixes, facility address "
    "repairs, compliance tracking, production fixes, billing warnings, bulk dispatch "
    "updates, background task fixes, test-data review, client details correction, "
    "user action recording, HIPAA/NIST tracking, production releases, billing "
    "requests, and audit entries."
)
SHORT = "TIPS and Harris bid strategy, law enforcement bids, and Canada follow-ups."


def test_a_line_inside_both_budgets_is_left_alone():
    assert not _too_long(SHORT)


def test_a_word_count_alone_would_let_a_long_worded_line_through():
    """Why there are two bounds and not just a word budget."""
    assert len(LONG_WORDS.split()) <= _DESC_MAX_WORDS
    assert len(LONG_WORDS) > _DESC_MAX_CHARS
    assert _too_long(LONG_WORDS)


def test_the_character_bound_is_enforced_not_merely_measured():
    out = _trim_to_phrase(LONG_WORDS)
    assert not _too_long(out)
    assert len(out) <= _DESC_MAX_CHARS


def test_a_line_over_both_bounds_is_caught():
    assert len(LONG.split()) > _DESC_MAX_WORDS
    assert len(LONG) > _DESC_MAX_CHARS
    assert _too_long(LONG)


def test_trimming_keeps_whole_items_and_fits():
    out = _trim_to_phrase(LONG)
    assert not _too_long(out)
    assert out.endswith(".")
    # Every kept item is a complete phrase from the original, not a mid-phrase cut.
    for item in out.rstrip(".").split(","):
        cleaned = item.strip().removeprefix("and ").strip()
        assert cleaned in LONG, cleaned


def test_trimming_reads_as_a_list_rather_than_stopping_dead():
    out = _trim_to_phrase(LONG)
    assert ", and " in out


def test_a_single_item_longer_than_the_budget_is_still_returned_whole():
    """Better one over-long true item than an empty project cell."""
    one = "A single enormous deliverable " * 12
    out = _trim_to_phrase(one.strip() + ".")
    assert out.strip(". ")


class _Condenser:
    """Stands in for Gemini's condense pass."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def __call__(self, _prompt):
        self.calls += 1
        return self.reply


def _fitted(monkeypatch, text, reply):
    gem = GeminiSummarizer("key")
    condenser = _Condenser(reply)
    monkeypatch.setattr(gem, "_generate_text", condenser)
    return gem._fit(text), condenser


def test_fit_does_not_call_the_model_for_a_line_that_already_fits(monkeypatch):
    out, condenser = _fitted(monkeypatch, SHORT, "something else entirely")
    assert out == SHORT
    assert condenser.calls == 0


def test_fit_takes_the_condensed_version_when_it_fits(monkeypatch):
    out, condenser = _fitted(monkeypatch, LONG, SHORT)
    assert out == SHORT
    assert condenser.calls == 1


def test_fit_falls_back_to_trimming_when_the_model_will_not_shorten_it(monkeypatch):
    """A refusal, an echo of the input or another over-long answer must not reach
    the sheet just because the model replied."""
    out, _ = _fitted(monkeypatch, LONG, LONG)
    assert not _too_long(out)
    assert out != LONG


def test_fit_falls_back_to_trimming_when_the_model_is_unavailable(monkeypatch):
    out, _ = _fitted(monkeypatch, LONG, None)
    assert not _too_long(out)


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(self.payload)}]}}]}


def test_fit_is_applied_to_every_description_the_batch_returns(monkeypatch):
    """The budget belongs in summarize_batch, not in each caller — redo_description
    and run.py both go through it, so neither can forget to apply it."""
    gem = GeminiSummarizer("key")
    monkeypatch.setattr(
        summarizer.requests,
        "post",
        lambda *a, **k: _FakeResponse({"Rafael Casabianca": LONG, "Dana Hette": SHORT}),
    )
    # The condense pass is a separate text call; make it unhelpful so the
    # hand-trim has to carry the over-long one.
    monkeypatch.setattr(gem, "_generate_text", lambda _p: None)

    out = gem.summarize_batch([("Rafael Casabianca", ["x"]), ("Dana Hette", ["y"])])
    assert set(out) == {"Rafael Casabianca", "Dana Hette"}
    for name, text in out.items():
        assert not _too_long(text), f"{name}: {len(text)} chars, {len(text.split())} words"
    assert out["Dana Hette"] == SHORT, "a line that already fits must pass through untouched"
