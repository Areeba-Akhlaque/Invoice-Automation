"""EXECUTION — Google Sheets adapter.

Auth-agnostic: takes a credentials object (service account or OAuth) built by
execution.config.google_credentials. Reads the workbook, duplicates an invoice
tab, writes cells, hides/unhides rows, and adds review notes.

Every API call goes through _retry: the scheduled run used to die on transient
DNS blips and connection resets (see scheduled.log), which left no invoice and
no alert.
"""
from __future__ import annotations

import random
import socket
import ssl
import time

import requests
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

_RETRYABLE_EXC = (
    socket.gaierror,
    ssl.SSLError,
    TimeoutError,
    ConnectionError,
    requests.exceptions.RequestException,
    OSError,
)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable_http(e: HttpError) -> bool:
    status = getattr(e, "status_code", None) or getattr(getattr(e, "resp", None), "status", None)
    return status in _RETRYABLE_STATUS


def _retry(fn, *, attempts: int = 4, what: str = "Sheets call"):
    """Run fn() with exponential backoff on transient network/API errors."""
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except HttpError as e:
            if not _is_retryable_http(e):
                raise  # 400/403/404 are real bugs — fail fast, don't mask them
            last = e
        except _RETRYABLE_EXC as e:
            last = e
        if i < attempts - 1:
            delay = 2**i + random.uniform(0, 0.5)
            print(f"  ! {what} failed ({str(last)[:90]}) — retry {i + 1}/{attempts - 1} in {delay:.1f}s")
            time.sleep(delay)
    raise last  # type: ignore[misc]


class SheetsClient:
    def __init__(self, spreadsheet_id: str, credentials):
        self.sid = spreadsheet_id
        self.svc = build("sheets", "v4", credentials=credentials)
        self._meta_cache = None

    # ---- metadata -------------------------------------------------------
    def meta(self, refresh: bool = False) -> dict:
        if self._meta_cache is None or refresh:
            self._meta_cache = _retry(
                lambda: self.svc.spreadsheets().get(spreadsheetId=self.sid).execute(),
                what="spreadsheet metadata",
            )
        return self._meta_cache

    def sheet_titles(self, refresh: bool = False) -> list[str]:
        return [s["properties"]["title"] for s in self.meta(refresh)["sheets"]]

    def sheet_id(self, title: str) -> int | None:
        for s in self.meta()["sheets"]:
            if s["properties"]["title"] == title:
                return s["properties"]["sheetId"]
        return None

    def sheet_position(self, title: str) -> int | None:
        for s in self.meta()["sheets"]:
            if s["properties"]["title"] == title:
                return s["properties"]["index"]
        return None

    # ---- read/write -----------------------------------------------------
    def get_values(
        self, a1_range: str, formulas: bool = False, unformatted: bool = False
    ) -> list[list]:
        """formulas -> raw '=...' text; unformatted -> real numbers (not '$1,234.00')."""
        opt = "FORMULA" if formulas else ("UNFORMATTED_VALUE" if unformatted else "FORMATTED_VALUE")
        return _retry(
            lambda: self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sid, range=a1_range, valueRenderOption=opt)
            .execute()
            .get("values", []),
            what=f"read {a1_range}",
        )

    def update(self, a1_range: str, values: list[list]):
        _retry(
            lambda: self.svc.spreadsheets()
            .values()
            .update(
                spreadsheetId=self.sid,
                range=a1_range,
                valueInputOption="USER_ENTERED",
                body={"values": values},
            )
            .execute(),
            what=f"update {a1_range}",
        )

    def batch_update_cells(self, updates: list[tuple[str, object]], tab: str):
        """updates = [(A1_cell, value), ...] all within `tab`."""
        if not updates:
            return
        data = [{"range": f"'{tab}'!{cell}", "values": [[val]]} for cell, val in updates]
        _retry(
            lambda: self.svc.spreadsheets()
            .values()
            .batchUpdate(
                spreadsheetId=self.sid,
                body={"valueInputOption": "USER_ENTERED", "data": data},
            )
            .execute(),
            what=f"write {len(updates)} cells to {tab}",
        )

    # ---- structure ------------------------------------------------------
    def duplicate_sheet(self, src_title: str, new_title: str, insert_index: int | None = None):
        src_id = self.sheet_id(src_title)
        if src_id is None:
            raise RuntimeError(f"Source tab not found: {src_title}")
        if insert_index is None:  # default: right AFTER the source tab (keep with its group)
            pos = self.sheet_position(src_title)
            insert_index = (pos + 1) if pos is not None else 0
        _retry(
            lambda: self.svc.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.sid,
                body={
                    "requests": [
                        {
                            "duplicateSheet": {
                                "sourceSheetId": src_id,
                                "newSheetName": new_title,
                                "insertSheetIndex": insert_index,
                            }
                        }
                    ]
                },
            )
            .execute(),
            what=f"duplicate {src_title} -> {new_title}",
        )
        self._meta_cache = None

    def move_sheet(self, title: str, new_index: int):
        sid = self.sheet_id(title)
        if sid is None:
            raise RuntimeError(f"Tab not found: {title}")
        _retry(
            lambda: self.svc.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.sid,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": {"sheetId": sid, "index": new_index},
                                "fields": "index",
                            }
                        }
                    ]
                },
            )
            .execute(),
            what=f"move {title}",
        )
        self._meta_cache = None

    def delete_sheet(self, title: str):
        sid = self.sheet_id(title)
        if sid is None:
            raise RuntimeError(f"Tab not found: {title}")
        _retry(
            lambda: self.svc.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.sid,
                body={"requests": [{"deleteSheet": {"sheetId": sid}}]},
            )
            .execute(),
            what=f"delete {title}",
        )
        self._meta_cache = None

    # ---- rows / notes ---------------------------------------------------
    def hidden_rows(self, title: str, start_row: int, end_row: int) -> set[int]:
        """Returns 1-based row numbers in [start_row, end_row] hidden by the user."""
        res = _retry(
            lambda: self.svc.spreadsheets()
            .get(
                spreadsheetId=self.sid,
                ranges=[f"'{title}'!A{start_row}:A{end_row}"],
                includeGridData=True,
                fields="sheets(data(startRow,rowMetadata(hiddenByUser)))",
            )
            .execute(),
            what=f"hidden rows of {title}",
        )
        data = res["sheets"][0]["data"][0]
        base = data.get("startRow", start_row - 1)
        rm = data.get("rowMetadata", [])
        return {base + i + 1 for i, m in enumerate(rm) if m.get("hiddenByUser")}

    def set_rows_hidden(self, title: str, changes: list[tuple[int, bool]]):
        """changes = [(row_1based, hidden_bool), ...]"""
        if not changes:
            return
        sid = self.sheet_id(title)
        reqs = [
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sid,
                        "dimension": "ROWS",
                        "startIndex": row - 1,
                        "endIndex": row,
                    },
                    "properties": {"hiddenByUser": hidden},
                    "fields": "hiddenByUser",
                }
            }
            for row, hidden in changes
        ]
        _retry(
            lambda: self.svc.spreadsheets()
            .batchUpdate(spreadsheetId=self.sid, body={"requests": reqs})
            .execute(),
            what=f"row visibility on {title}",
        )

    def set_cell_note(self, title: str, row: int, col_letter: str, note: str):
        """Adds/overwrites a cell note at the given 1-based row + column letter."""
        sid = self.sheet_id(title)
        col = 0
        for ch in col_letter.upper():
            col = col * 26 + (ord(ch) - 64)
        col -= 1
        _retry(
            lambda: self.svc.spreadsheets()
            .batchUpdate(
                spreadsheetId=self.sid,
                body={
                    "requests": [
                        {
                            "updateCells": {
                                "range": {
                                    "sheetId": sid,
                                    "startRowIndex": row - 1,
                                    "endRowIndex": row,
                                    "startColumnIndex": col,
                                    "endColumnIndex": col + 1,
                                },
                                "rows": [{"values": [{"note": note}]}],
                                "fields": "note",
                            }
                        }
                    ]
                },
            )
            .execute(),
            what=f"note on {title}!{col_letter}{row}",
        )
