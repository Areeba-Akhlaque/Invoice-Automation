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
```

Flow is one-directional: `run.py → orchestration → execution` (the directive
layer is data that both read).

## How it works (high level)
- Pulls hours from the time-tracking source for the relevant period.
- Writes one concise description per person from their time entries.
- Duplicates the latest invoice tab and fills only the variable cells, so the
  sheet's own rate/formula logic is preserved.
- The total is the calculated subtotal; a reviewer applies any agreed adjustment.

## Setup
Provide these as **GitHub repository secrets** (Settings → Secrets and variables →
Actions), or in a local `.env` for local runs:

| Secret | Purpose |
|---|---|
| `KIMAI_URL` | Kimai base URL |
| `KIMAI_TOKEN` | Kimai API token |
| `GEMINI_API_KEY` | Gemini API key (AI descriptions) |
| `GOOGLE_TOKEN_JSON` | contents of an OAuth `token.json` with the Sheets scope |

Run:
```bash
pip install -r requirements.txt
python run.py                  # preview (writes nothing)
python run.py --write          # create the tab
python check_setup.py          # verify connectivity
```

## Scheduling
`.github/workflows/invoice.yml` runs on a cron (twice a month) or via the Actions
**Run workflow** button (with a write toggle). No server or laptop required.

## Security
Secrets (`.env`, `token.json`, client secrets) are gitignored and never committed.
**Keep this repository private** — configuration under `directive/` contains
business data.
