# Directive layer

The **what** — declarative configuration and billing policy. No logic here; the
Orchestration layer reads these files and the Execution layer enforces them.

| File | Holds |
|---|---|
| `settings.yaml` | spreadsheet id, invoice rules, schedule (run days), Gemini model, sheet cell layout, pass-through rows |
| `roster.yaml` | each person: rate, markup, `hours_source` (fixed/kimai/manual), `kimai_user_id` |

## The billing directives (Cherry's model)
- **Advance billing:** invoice for period `[S,E]` is issued `S+7` (1-15 → 8th, 16-30 → 23rd); due = `E`. Automation runs the day before issue (**7th & 22nd**).
- **Full-timers** → `86.5` hrs (estimate). **Hourly** (Dana, Clarissa, Alex, Prameeth) → Kimai actuals from the **previous complete half-month**. **James/Bradd/Keeko** → carried over from the previous invoice (estimate, adjusted later).
- **Descriptions** → AI-summarized from each person's Kimai entries in `[issue-15, issue-1]`. Missing/repetitive → left empty + a review note.
- **Total** = calculated subtotal (no auto-cap). The reviewer (Cherry) applies the agreed-amount discount manually.
- **Hidden rows** with leftover values are cleared so the subtotal always equals the visible line items.

To change behaviour, edit these files — not the code.
