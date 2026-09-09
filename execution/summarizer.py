"""EXECUTION — Gemini summarizer. Turns a person's Kimai timesheet entries into
one concise, Cherry-style invoice description. Batched (1 call for everyone) to
stay within the free-tier daily quota; falls back to a raw join on failure.
"""
from __future__ import annotations

import json
import time

import requests

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

_BATCH_PROMPT = """You write the one-line "project" cell for people on a consulting invoice.

House style for EACH description (do not deviate):
- ONE line, comma-separated work areas / deliverables (noun phrases).
- NOT full sentences. Do NOT start with a verb (Developed/Managed/Worked) or the person's name.
- Optionally a short theme + "including ...". No links/IDs/ticket numbers. ~25 words / ~160 chars. End with a period.

Style example: "Security audits, PHI logging, AWS Macie, Docker cleanup, SMS compliance fixes, S3 backups, and Help Center embedding."

Below are several people, each with their raw time entries. Return ONLY a JSON
object mapping each person's EXACT name (as the heading) to their one-line
description. No other text.

{blocks}
"""

_PROJECTS_PROMPT = """You write the "PROJECTS ON THIS INVOICE" paragraph for a consulting invoice.

Below are the per-person "project" cells from this invoice. Condense them into ONE
short, cohesive paragraph summarizing the period's work as a whole.

Rules (do not deviate):
- LENGTH IS A HARD LIMIT: the paragraph MUST be at most {max_words} words (aim for
  about {words}). Being over the limit is a failure. Count your words before answering.
- Do NOT enumerate every deliverable. ABSTRACT the entries into a handful of themes
  (engineering, QA, project management, security/infrastructure, customer support, ...)
  and describe each theme in a few words. Drop minor/one-off details to stay in budget.
- ONE paragraph of plain prose. No people's names, no bullets, no headings, no line breaks.
- Describe ONLY work that appears in the entries below. No links/IDs/ticket numbers.
- Match the tone and structure of the previous invoice's paragraph below (same size,
  same level of abstraction) — but write this period's content, do not copy it.

PREVIOUS INVOICE'S PARAGRAPH (style/length reference only — its content is from
another period, do not copy it):
{example}

THIS INVOICE'S ENTRIES:
{blocks}

Return ONLY the paragraph text, at most {max_words} words.
"""


class GeminiSummarizer:
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash-lite"):
        self.api_key = api_key
        self.model = model

    @staticmethod
    def _dedupe(entries: list[str], cap: int = 40) -> list[str]:
        seen, uniq = set(), []
        for e in entries:
            k = " ".join(e.split()).strip().lower()
            if k and k not in seen:
                seen.add(k)
                uniq.append(" ".join(e.split()).strip())
        return uniq[:cap]

    @staticmethod
    def _key(name: str) -> str:
        return " ".join(str(name).split()).strip().lower()

    def _summarize_one(self, name: str, entries: list[str]) -> str | None:
        """Single-person retry for anyone the batch call dropped."""
        block = f"### {name}\n" + "\n".join(f"- {e}" for e in self._dedupe(entries))
        text = self._generate_text(
            _BATCH_PROMPT.format(blocks=block).replace(
                "Return ONLY a JSON\nobject mapping each person's EXACT name (as the heading) to their one-line\ndescription. No other text.",
                "Return ONLY the one-line description. No JSON, no name, no other text.",
            )
        )
        if not text:
            return None
        text = text.strip().strip('"').strip()
        return text or None

    def summarize_batch(self, people: list[tuple[str, list[str]]]) -> dict[str, str]:
        """One API call for many people. people = [(name, entries)]. Returns {name: summary}.

        Anyone the batch drops is retried individually; anyone still missing is
        OMITTED from the result so the caller flags the cell for manual review.
        It must never fall back to raw Kimai text — those are internal notes
        ("Auth is down! Fixing now") and this cell goes to the client.
        """
        if not people:
            return {}
        blocks = []
        for name, entries in people:
            uniq = self._dedupe(entries)
            blocks.append(f"### {name}\n" + "\n".join(f"- {e}" for e in uniq))
        prompt = _BATCH_PROMPT.format(blocks="\n\n".join(blocks))

        result: dict[str, str] = {}
        for attempt in range(3):
            try:
                resp = requests.post(
                    GEMINI_URL.format(model=self.model),
                    params={"key": self.api_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.3,
                            "maxOutputTokens": 2048,
                            "thinkingConfig": {"thinkingBudget": 0},
                            "responseMimeType": "application/json",
                        },
                    },
                    timeout=90,
                )
                if resp.status_code in (429, 500, 503):
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                parsed = json.loads(text)
                result = {k: " ".join(str(v).split()).strip() for k, v in parsed.items()}
                break
            except Exception as e:  # noqa: BLE001
                print(f"  ! batch summarize attempt {attempt + 1} failed: {str(e)[:100]}")
                time.sleep(3 * (attempt + 1))

        # Gemini often echoes the heading with different spacing/case — match loosely.
        by_key = {self._key(k): v for k, v in result.items() if str(v).strip()}
        out: dict[str, str] = {}
        missing: list[tuple[str, list[str]]] = []
        for name, entries in people:
            got = by_key.get(self._key(name))
            if got:
                out[name] = got
            else:
                missing.append((name, entries))

        for name, entries in missing:
            got = self._summarize_one(name, entries)
            if got:
                out[name] = got
            else:
                print(f"  ! no AI description for {name} — cell will be flagged for review")
        return out

    def _generate_text(self, prompt: str) -> str | None:
        """One text-generation call with retry on transient errors. None on failure."""
        for attempt in range(3):
            try:
                resp = requests.post(
                    GEMINI_URL.format(model=self.model),
                    params={"key": self.api_key},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": 0.3,
                            "maxOutputTokens": 1024,
                            "thinkingConfig": {"thinkingBudget": 0},
                        },
                    },
                    timeout=90,
                )
                if resp.status_code in (429, 500, 503):
                    time.sleep(3 * (attempt + 1))
                    continue
                resp.raise_for_status()
                text = resp.json()["candidates"][0]["content"]["parts"][0]["text"]
                text = " ".join(text.split()).strip()
                if text:
                    return text
            except Exception as e:  # noqa: BLE001
                print(f"  ! projects summary attempt {attempt + 1} failed: {str(e)[:100]}")
                time.sleep(3 * (attempt + 1))
        return None

    def summarize_projects(self, blurbs: list[str], example: str = "") -> str | None:
        """Condenses all per-person project cells into the one "PROJECTS ON THIS
        INVOICE" paragraph, sized like `example` (the previous invoice's paragraph).
        Returns None on failure (caller keeps the carried-over text)."""
        if not blurbs:
            return None
        example = " ".join(example.split()).strip()
        words = min(max(len(example.split()), 60), 100) if example else 90
        max_words = words + 15
        prompt = _PROJECTS_PROMPT.format(
            words=words,
            max_words=max_words,
            example=example or "(not available — use a neutral professional tone)",
            blocks="\n".join(f"- {b}" for b in blurbs),
        )
        text = self._generate_text(prompt)
        if not text:
            return None
        # Enforce the length ceiling: if the model overshot, ask it once to compress
        # (keeps whole sentences, unlike a hard truncation). Keep the shorter of the two.
        if len(text.split()) > max_words:
            shorter = self._generate_text(
                f"Rewrite the following paragraph as ONE paragraph of at most {max_words} "
                f"words (target ~{words}), keeping the most important themes and dropping "
                f"minor detail. Plain prose, no names/bullets/headings. Return ONLY the "
                f"paragraph.\n\n{text}"
            )
            if shorter and len(shorter.split()) < len(text.split()):
                text = shorter
        return text
