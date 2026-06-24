"""EXECUTION — loads the Directive layer (YAML), .env credentials, and resolves
Google credentials (service account for cloud/CI, OAuth installed-app for local).
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DIRECTIVE_DIR = ROOT / "directive"


def _normalize_name(name: str) -> str:
    """Whitespace/case-insensitive key for matching people across sheet and config."""
    return " ".join(str(name).split()).strip().lower()


def load_settings() -> dict:
    with open(DIRECTIVE_DIR / "settings.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_roster() -> list[dict]:
    with open(DIRECTIVE_DIR / "roster.yaml", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    roster = data["laborers"]
    for person in roster:
        person["_key"] = _normalize_name(person["name"])
        person["bill_rate"] = round(person["base_rate"] * person.get("markup", 1.0), 4)
    return roster


def roster_by_key(roster: list[dict]) -> dict:
    return {p["_key"]: p for p in roster}


def load_kimai_env() -> dict:
    load_dotenv(ROOT / ".env", override=False)  # no-op in CI; uses real env there
    url = (os.getenv("KIMAI_URL") or os.getenv("KIMAI_API_URL") or "").strip().rstrip("/")
    if url.lower().endswith("/api"):
        url = url[:-4]
    token = (os.getenv("KIMAI_TOKEN") or os.getenv("KIMAI_API_TOKEN") or "").strip()
    if not url or not token:
        raise RuntimeError("KIMAI_URL / KIMAI_TOKEN missing (set in .env or as env/secrets).")
    return {
        "url": url,
        "token": token,
        "user": os.getenv("KIMAI_USER", "").strip() or None,
        "verify_ssl": os.getenv("KIMAI_VERIFY_SSL", "true").strip().lower() != "false",
    }


def load_gemini_key() -> str:
    load_dotenv(ROOT / ".env", override=False)
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY missing (set in .env or as env/secret).")
    return key


def google_credentials(settings: dict):
    """Resolve Google credentials.

    Priority (cloud/CI friendly):
      1. GOOGLE_SERVICE_ACCOUNT_JSON  — service-account JSON as a string (GitHub secret)
      2. GOOGLE_APPLICATION_CREDENTIALS / GOOGLE_SERVICE_ACCOUNT_FILE — path to SA json
      3. OAuth installed-app flow with client_secret + token.json (local, interactive)
    """
    load_dotenv(ROOT / ".env", override=False)
    scopes = settings["google"]["scopes"]

    sa_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    sa_file = os.getenv("GOOGLE_APPLICATION_CREDENTIALS") or os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    if sa_json or (sa_file and Path(sa_file).exists()):
        from google.oauth2 import service_account

        if sa_json:
            info = json.loads(sa_json)
            return service_account.Credentials.from_service_account_info(info, scopes=scopes)
        return service_account.Credentials.from_service_account_file(sa_file, scopes=scopes)

    # --- OAuth installed-app fallback (local dev) ---
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    g = settings["google"]
    token_path = ROOT / g["token_file"]
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            matches = glob.glob(str(ROOT / g["client_secret_glob"]))
            if not matches:
                raise RuntimeError(
                    "No Google credentials: set GOOGLE_SERVICE_ACCOUNT_JSON, or place a "
                    f"client secret matching {g['client_secret_glob']} for local OAuth."
                )
            flow = InstalledAppFlow.from_client_secrets_file(matches[0], scopes)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds
