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

    def summarize_batch(self, people: list[tuple[str, list[str]]]) -> dict[str, str]:
        """One API call for many people. people = [(name, entries)]. Returns {name: summary}.

        Falls back to per-person raw joins for anyone missing from the response.
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

        out: dict[str, str] = {}
        for name, entries in people:
            out[name] = result.get(name) or "; ".join(self._dedupe(entries, 8))[:500]
        return out
