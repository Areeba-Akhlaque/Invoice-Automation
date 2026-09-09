"""EXECUTION — minimal Kimai REST client.

Targets Kimai 2.x (Bearer API token). Falls back to legacy X-AUTH headers when a
KIMAI_USER is provided. Pulls timesheets for a date range and aggregates per user.
"""
from __future__ import annotations

import random
import time

import requests

_RETRY_STATUS = {429, 500, 502, 503, 504}


class KimaiClient:
    def __init__(self, url: str, token: str, user: str | None = None, verify_ssl: bool = True):
        self.base = url.rstrip("/")
        self.verify = verify_ssl
        self.session = requests.Session()
        if user:  # legacy Kimai 1.x header auth
            self.session.headers.update({"X-AUTH-USER": user, "X-AUTH-TOKEN": token})
        else:  # Kimai 2.x bearer token
            self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.session.headers.update({"Accept": "application/json"})

    def _get(self, path: str, attempts: int = 4, **params):
        last: Exception | None = None
        for i in range(attempts):
            try:
                resp = self.session.get(
                    f"{self.base}/api{path}", params=params, verify=self.verify, timeout=60
                )
                if resp.status_code in _RETRY_STATUS and i < attempts - 1:
                    last = requests.HTTPError(f"HTTP {resp.status_code}")
                else:
                    resp.raise_for_status()
                    return resp.json()
            except requests.exceptions.RequestException as e:
                if i == attempts - 1:
                    raise
                last = e
            delay = 2**i + random.uniform(0, 0.5)
            print(f"  ! Kimai {path} failed ({str(last)[:70]}) — retrying in {delay:.1f}s")
            time.sleep(delay)
        raise last  # type: ignore[misc]

    def ping(self) -> dict:
        """Cheap auth check — returns the current user."""
        return self._get("/users/me")

    def users(self) -> list[dict]:
        return self._get("/users")

    def timesheets(self, begin: str, end: str, user: str = "all", size: int = 500) -> list[dict]:
        """begin/end are ISO local datetimes: 'YYYY-MM-DDTHH:MM:SS'."""
        rows: list[dict] = []
        page = 1
        while True:
            try:
                batch = self._get(
                    "/timesheets", begin=begin, end=end, user=user, size=size, page=page
                )
            except requests.HTTPError as e:
                # Kimai answers 404 for a page past the last one when the total is
                # an exact multiple of `size` — that is the end, not a failure.
                if getattr(e.response, "status_code", None) == 404 and page > 1:
                    break
                raise
            if not batch:
                break
            rows.extend(batch)
            if len(batch) < size:
                break
            page += 1
        return rows

    def hours_with_identity(self, begin: str, end: str) -> list[dict]:
        """Returns [{id, username, alias, hours}] aggregated over the period."""
        id_map = {u["id"]: u for u in self.users()}
        seconds: dict[int, float] = {}
        for t in self.timesheets(begin, end):
            uid = t.get("user")
            seconds[uid] = seconds.get(uid, 0) + (t.get("duration") or 0)

        rows = []
        for uid, sec in seconds.items():
            u = id_map.get(uid, {})
            rows.append(
                {
                    "id": uid,
                    "username": u.get("username"),
                    "alias": u.get("alias"),
                    "hours": round(sec / 3600.0, 2),
                }
            )
        return rows

    def descriptions_by_user(
        self, begin: str, end: str, user_id: int | None = None
    ) -> dict[int, list[str]]:
        """Returns {user_id: [entry descriptions]} for the period (non-empty only).

        Pass user_id to fetch just one person (used by --desc-window, which would
        otherwise pull every user's timesheets to read a single person's).
        """
        out: dict[int, list[str]] = {}
        for t in self.timesheets(begin, end, user=str(user_id) if user_id else "all"):
            desc = (t.get("description") or "").strip()
            if desc:
                out.setdefault(t.get("user"), []).append(desc)
        return out
