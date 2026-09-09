#!/usr/bin/env python
"""Re-authorise the local Google token with the scopes in directive/settings.yaml.

Needed after a scope is added (e.g. calendar.readonly for hours_source: calendar)
— an existing token.json keeps working for the old scopes and then fails with a
confusing 403 on the new API.

    python tools/reauth.py

Opens a browser; sign in as the account that can see the invoice sheet AND the
calendars named in roster.yaml. Afterwards, if the cloud run uses GOOGLE_TOKEN_JSON,
paste the new token.json into that repository secret.
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402

from execution.config import ROOT, load_settings  # noqa: E402


def main() -> None:
    settings = load_settings()
    g = settings["google"]
    scopes = g["scopes"]
    token_path = ROOT / g["token_file"]

    matches = glob.glob(str(ROOT / g["client_secret_glob"]))
    if not matches:
        sys.exit(f"No client secret matching {g['client_secret_glob']} in {ROOT}.")

    print("Requesting scopes:")
    for s in scopes:
        print(f"  - {s}")
    print("\nA browser window will open — approve every requested permission.\n")

    flow = InstalledAppFlow.from_client_secrets_file(matches[0], scopes)
    creds = flow.run_local_server(port=0)

    if not creds.has_scopes(scopes):
        granted = set(creds.scopes or [])
        sys.exit("Not all scopes were granted: missing " + ", ".join(s for s in scopes if s not in granted))

    token_path.write_text(creds.to_json(), encoding="utf-8")
    print(f"\nWrote {token_path} with all {len(scopes)} scopes.")
    print("If the GitHub Actions run uses GOOGLE_TOKEN_JSON, update that secret with this file.")


if __name__ == "__main__":
    main()
