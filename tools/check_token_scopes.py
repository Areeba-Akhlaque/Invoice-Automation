#!/usr/bin/env python
"""Verify a restored token.json carries every scope directive/settings.yaml needs.

Run by the workflow right after writing the secret to disk, so a stale token
fails immediately with a readable message instead of 403-ing mid-run. Adding
calendar.readonly invalidated every token minted before it, and the old failure
mode was an opaque "insufficient authentication scopes" deep inside an API call.

    python tools/check_token_scopes.py [path/to/token.json]

Exit 0 if every scope is present, 1 otherwise.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from execution.config import load_settings  # noqa: E402


def main() -> int:
    token_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "token.json"
    if not token_path.exists():
        print(f"::error::{token_path} does not exist")
        return 1
    try:
        have = set(json.loads(token_path.read_text(encoding="utf-8")).get("scopes") or [])
    except json.JSONDecodeError as e:
        print(f"::error::{token_path} is not valid JSON ({e}) — the secret is probably truncated")
        return 1

    need = set(load_settings()["google"]["scopes"])
    missing = sorted(need - have)
    if missing:
        print(
            "::error::token is missing scope(s): "
            + ", ".join(missing)
            + " — run `python tools/reauth.py` locally, then update the GOOGLE_TOKEN_JSON secret"
        )
        return 1
    print(f"token OK: all {len(need)} required scope(s) present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
