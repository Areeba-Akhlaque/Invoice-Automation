# DRC Invoice Automation

Auto-generates the next **David Roberts Consulting** invoice tab in the Google
Sheet *Time Log & Invoice Generator* — pulling dev hours from **Kimai**, writing
AI project descriptions (**Gemini**), and reusing the sheet's own rate / markup /
discount formulas. Runs locally (Windows Task Scheduler) or in the cloud
(**GitHub Actions** — no laptop needed).

## Architecture — DOE (Directive · Orchestration · Execution)

```
directive/        # WHAT  — declarative config & billing policy (edit here, not the code)
  settings.yaml   #         sheet id, invoice rules, schedule, Gemini model, cell layout
  roster.yaml     #         people: rate, markup, hours_source, kimai_user_id
orchestration/    # HOW   — coordination (no integrations of its own)
  schedule.py     #         billing calendar: periods, issue date, estimate/description windows
  orchestrator.py #         pipeline: resolve windows -> pull Kimai -> summarize -> build plan -> commit
execution/        # DO    — adapters/workers that touch the outside world
  config.py       #         loads directives + env + Google credentials (service account / OAuth)
  kimai.py        #         Kimai REST client
  sheets.py       #         Google Sheets adapter
  summarizer.py   #         Gemini description summarizer (batched)
  invoice.py      #         builds the plan & writes the tab
run.py            # entry point (CLI + preview)
check_setup.py    # connectivity check
.github/workflows/invoice.yml   # cloud cron (7th & 22nd) + manual run
```

The flow is one-directional: **run.py → orchestration → execution → (directive is data read by both)**.

## Billing model (Cherry-confirmed, advance billing)
- Invoice for `[S,E]` is **issued S+7** (1-15 → 8th, 16-30 → 23rd); **due = E**. Automation runs the day before issue (**7th & 22nd**).
- **Full-timers** → 86.5 hrs. **Hourly** (Dana, Clarissa, Alex, Prameeth) → Kimai actuals from the **previous complete half-month**. **James/Bradd/Keeko** → carried over from the previous invoice (estimate; adjust later or pass `--manual`).
- **Descriptions** → AI-summarized from each person's Kimai entries in `[issue-15, issue-1]`; missing/repetitive are left empty with a review note.
- **Total** = calculated subtotal (no auto-cap). The reviewer applies the agreed-amount discount. Hidden rows' leftover values are cleared so the subtotal == visible line items.

## Usage
```bash
pip install -r requirements.txt

python run.py                                   # preview next invoice (auto period)
python run.py --start 2026-07-01 --end 2026-07-15            # preview a specific period
python run.py --start 2026-07-01 --end 2026-07-15 \
    --manual "James Hereford=116,Bradd Schofield=50" \
    --passthrough "Google Cloud Platform=1642.46" --write    # write the tab
python check_setup.py                           # verify Kimai + Google + roster
```

## Setup — Local (interactive)
1. `cp .env.example .env` and fill `KIMAI_*` + `GEMINI_API_KEY`.
2. Keep a `client_secret_*.json` in the project root; first run opens a browser and caches `token.json`.
3. Optional Windows schedule: `run_invoice.bat` via Task Scheduler (daily; `--auto` only acts on 7th/22nd). Needs the laptop on.

## Setup — Cloud (GitHub Actions, no laptop)
1. **Push this repo to GitHub** (secrets are gitignored — safe).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add:
   - `KIMAI_URL`, `KIMAI_TOKEN`, `GEMINI_API_KEY`
   - `GOOGLE_TOKEN_JSON` — paste the entire contents of your local `token.json` (it
     holds the OAuth refresh token; the workflow restores it and refreshes headlessly).
3. Done. `.github/workflows/invoice.yml` runs on the **7th & 22nd at 9am PST** (17:00 UTC),
   or trigger manually from the **Actions** tab (with an optional “write” toggle).

> **OAuth longevity:** for the refresh token to keep working long-term, set the
> OAuth consent screen to **Publishing status: In production** (Google Cloud Console
> → APIs & Services → OAuth consent screen → Publish App). "Testing" refresh tokens
> expire after 7 days. (A service account avoids this entirely — supported via
> `GOOGLE_SERVICE_ACCOUNT_JSON` if you ever want it.)

## Notes
- Gemini uses **one batched call** per run (well within the free daily quota).
- The scheduled run produces a **draft** (Kimai + fixed + descriptions); the reviewer fills manual hours / pass-throughs and applies the agreed discount.
- Secrets (`.env`, `token.json`, `client_secret_*.json`, service-account JSON) are **never committed** — see `.gitignore`.
