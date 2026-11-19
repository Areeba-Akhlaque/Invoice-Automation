"""A scheduled run that finds the period already invoiced must not report failure.

The draft is reviewed on the 5th and the 20th, so it is routinely built a day or
two early by hand — and then the cron fires, finds the latest tab already covering
that period, and exits 1. That sent a "workflow failed" email twice a month for
work that was actually done (2026-11-05 is the last one), which is the fastest way
to teach everyone to ignore those emails. The same condition at a terminal IS a
failure: someone asked for a tab and did not get one.
"""
import runpy
import sys

import pytest

import run as run_module
from execution.invoice import PeriodAlreadyInvoiced

MESSAGE = (
    "'DRC-0070' is already an invoice for 11/16/2026 -> 11/30/2026. "
    "Creating DRC-0071 would bill the same period twice."
    "\nUse --allow-duplicate-period if this is intentional."
)


def test_it_is_still_a_runtime_error():
    """Existing handlers catch RuntimeError; narrowing the type must not slip the
    error past them and let a duplicate tab get written."""
    assert issubclass(PeriodAlreadyInvoiced, RuntimeError)


@pytest.fixture
def _raises_duplicate(monkeypatch):
    def boom(_opts):
        raise PeriodAlreadyInvoiced(MESSAGE)

    monkeypatch.setattr(run_module, "prepare", boom)
    monkeypatch.setattr(run_module, "load_settings", lambda: {"schedule": {"run_days": [5, 20]}})
    monkeypatch.setattr(run_module, "is_billing_day", lambda _days: True)


def _main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    try:
        run_module.main()
    except SystemExit as e:
        return e.code or 0
    return 0


def test_a_scheduled_run_exits_clean(monkeypatch, _raises_duplicate, capsys):
    code = _main(monkeypatch, ["--auto", "--write", "--yes"])
    assert code == 0, "the schedule must not report failure for an invoice already built"
    assert "Nothing to do" in capsys.readouterr().out


def test_a_scheduled_run_still_says_which_tab_and_period(monkeypatch, _raises_duplicate, capsys):
    """Exiting quietly is not the same as exiting silently — the log has to show
    why nothing was written."""
    _main(monkeypatch, ["--auto", "--write", "--yes"])
    out = capsys.readouterr().out
    assert "DRC-0070" in out
    assert "11/16/2026 -> 11/30/2026" in out


def test_an_interactive_run_still_fails(monkeypatch, _raises_duplicate):
    code = _main(monkeypatch, [])
    assert code != 0, "at a terminal, asking for a tab and not getting one is a failure"


def test_an_interactive_run_still_suggests_the_override(monkeypatch, _raises_duplicate):
    monkeypatch.setattr(sys, "argv", ["run.py"])
    with pytest.raises(SystemExit) as e:
        run_module.main()
    assert "--allow-duplicate-period" in str(e.value)


def test_the_module_is_runnable_as_a_script():
    """The workflow calls `python run.py`, so an import-time break would only show
    up in the cloud."""
    with pytest.raises(SystemExit):
        sys.argv = ["run.py", "--help"]
        runpy.run_module("run", run_name="__main__")
