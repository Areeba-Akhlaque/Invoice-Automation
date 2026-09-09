# Invoice Automation

Generates the next invoice tab in a Google Sheet from a time-tracking source
(**Kimai**) plus AI-written project descriptions (**Gemini**). Runs in the cloud
on a schedule (**GitHub Actions**) or locally.

## Architecture — DOE (Directive · Orchestration · Execution)

```
directive/        WHAT  — configuration & policy (settings, roster)
orchestration/    HOW   — billing calendar + pipeline coordination
execution/        DO    — Kimai, Google Sheets, Gemini, invoice builder
run.py            entry point (CLI + preview)
tools/            one-off maintenance scripts (not part of the pipeline)
tests/            pytest — pure logic + the tab writer against a fake sheet
```

Flow is one-directional: `run.py → orchestration → execution` (the directive
layer is data that both read).

## How it works (high level)
- Pulls hours from the time-tracking source for the relevant period.
- Writes one concise description per person from their time entries.
- Duplicates the latest invoice tab and fills only the variable cells, so the
  sheet's own rate/formula logic is preserved.
- The total is the calculated subtotal; a reviewer applies any agreed adjustment.

**The sheet is the source of truth.** Rates and any cell we don't write come from
the tab itself, so the preview shows what the sheet will actually compute —
including values carried over from the previous invoice, marked `(carried)`.

## Safety checks
The run aborts *before touching the sheet* when:
- the tab layout no longer matches `directive/settings.yaml` (the cell map drifted
  by three rows once and the discount was written into an empty cell for months);
- the previous tab already covers the period being invoiced (`--allow-duplicate-period`
  to override);
- a `--manual` / `--desc-window` name isn't in the roster (a typo used to be
  silently ignored, leaving that person on a stale estimate).

It warns loudly — **before and after the write** — when a roster name isn't found
on the tab, when a rate differs from `roster.yaml`, or when a description couldn't
be generated. If the write fails part-way, the half-built tab is deleted.

## Setup
Provide these as **GitHub repository secrets** (Settings → Secrets and variables →
Actions), or in a local `.env` for local runs:

| Secret | Purpose |
|---|---|
| `KIMAI_URL` | Kimai base URL |
| `KIMAI_TOKEN` | Kimai API token |
| `GEMINI_API_KEY` | Gemini API key (AI descriptions) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | service-account JSON — **preferred for the cloud run** |
| `GOOGLE_TOKEN_JSON` | fallback: contents of an OAuth `token.json` with the Sheets scope |

> Prefer the service account: share the sheet with its email as Editor. A user
> OAuth refresh token expires (after 7 days while the OAuth app is in *Testing*),
> which silently kills the scheduled run.

Run:
```bash
pip install -r requirements.txt
python run.py                  # preview (writes nothing)
python run.py --write          # create the tab
python check_setup.py          # verify connectivity

pip install -r requirements-dev.txt
pytest                         # 80+ tests, no network needed
ruff check .
```

### Useful flags
| Flag | What it does |
|---|---|
| `--manual "James Hereford=116"` | set hours for someone billed manually |
| `--passthrough "Google Cloud Platform=1642.46"` | set a third-party line |
| `--cap 51188` | agreed total; the discount cell fills the gap (blank = carry over the previous invoice's) |
| `--desc-window "Victor Cheung=2026-08-10:2026-08-24"` | pull *one* person's description from a different Kimai window (e.g. their default window is all PTO) |
| `--allow-duplicate-period` | proceed even though the previous tab covers this period |
| `--no-kimai` / `--no-descriptions` | skip those steps |

### One-off maintenance
```bash
# Re-summarize one person's description on an EXISTING tab, without regenerating it:
python tools/redo_description.py --person "Victor Cheung" \
    --from 2026-08-10 --to 2026-08-24 --tab DRC-0065          # preview
python tools/redo_description.py ... --write                   # apply
```

## Scheduling
`.github/workflows/invoice.yml` runs on a cron (twice a month) or via the Actions
**Run workflow** button (with a write toggle, period, cap and description-window
inputs). No server or laptop required. `.github/workflows/ci.yml` runs lint +
tests on every push.

## Security
Secrets (`.env`, `token.json`, client secrets) are gitignored and never committed.
Workflow inputs are passed through the environment, never interpolated into a
shell command.
**Keep this repository private** — configuration under `directive/` contains
business data.
