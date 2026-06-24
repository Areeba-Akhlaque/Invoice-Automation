"""EXECUTION — minimal Kimai REST client.

Targets Kimai 2.x (Bearer API token). Falls back to legacy X-AUTH headers when a
KIMAI_USER is provided. Pulls timesheets for a date range and aggregates per user.
"""
from __future__ import annotations

import requests


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

    def _get(self, path: str, **params):
        resp = self.session.get(
            f"{self.base}/api{path}", params=params, verify=self.verify, timeout=60
        )
        resp.raise_for_status()
        return resp.json()

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
            batch = self._get(
                "/timesheets", begin=begin, end=end, user=user, size=size, page=page
            )
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

    def descriptions_by_user(self, begin: str, end: str) -> dict[int, list[str]]:
        """Returns {user_id: [entry descriptions]} for the period (non-empty only)."""
        out: dict[int, list[str]] = {}
        for t in self.timesheets(begin, end):
            desc = (t.get("description") or "").strip()
            if desc:
                out.setdefault(t.get("user"), []).append(desc)
        return out
