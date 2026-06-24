"""EXECUTION — Google Sheets adapter.

Auth-agnostic: takes a credentials object (service account or OAuth) built by
execution.config.google_credentials. Reads the workbook, duplicates an invoice
tab, writes cells, hides/unhides rows, and adds review notes.
"""
from __future__ import annotations

from googleapiclient.discovery import build


class SheetsClient:
    def __init__(self, spreadsheet_id: str, credentials):
        self.sid = spreadsheet_id
        self.svc = build("sheets", "v4", credentials=credentials)
        self._meta_cache = None

    # ---- metadata -------------------------------------------------------
    def meta(self, refresh: bool = False) -> dict:
        if self._meta_cache is None or refresh:
            self._meta_cache = self.svc.spreadsheets().get(spreadsheetId=self.sid).execute()
        return self._meta_cache

    def sheet_titles(self) -> list[str]:
        return [s["properties"]["title"] for s in self.meta()["sheets"]]

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
    def get_values(self, a1_range: str, formulas: bool = False) -> list[list]:
        opt = "FORMULA" if formulas else "FORMATTED_VALUE"
        return (
            self.svc.spreadsheets()
            .values()
            .get(spreadsheetId=self.sid, range=a1_range, valueRenderOption=opt)
            .execute()
            .get("values", [])
        )

    def update(self, a1_range: str, values: list[list]):
        self.svc.spreadsheets().values().update(
            spreadsheetId=self.sid,
            range=a1_range,
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

    def batch_update_cells(self, updates: list[tuple[str, object]], tab: str):
        """updates = [(A1_cell, value), ...] all within `tab`."""
        data = [{"range": f"'{tab}'!{cell}", "values": [[val]]} for cell, val in updates]
        self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.sid,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        ).execute()

    # ---- structure ------------------------------------------------------
    def duplicate_sheet(self, src_title: str, new_title: str, insert_index: int | None = None):
        src_id = self.sheet_id(src_title)
        if src_id is None:
            raise RuntimeError(f"Source tab not found: {src_title}")
        if insert_index is None:  # default: right AFTER the source tab (keep with its group)
            pos = self.sheet_position(src_title)
            insert_index = (pos + 1) if pos is not None else 0
        self.svc.spreadsheets().batchUpdate(
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
        ).execute()
        self._meta_cache = None

    def move_sheet(self, title: str, new_index: int):
        sid = self.sheet_id(title)
        if sid is None:
            raise RuntimeError(f"Tab not found: {title}")
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sid, "index": new_index}, "fields": "index"}}]},
        ).execute()
        self._meta_cache = None

    def delete_sheet(self, title: str):
        sid = self.sheet_id(title)
        if sid is None:
            raise RuntimeError(f"Tab not found: {title}")
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,
            body={"requests": [{"deleteSheet": {"sheetId": sid}}]},
        ).execute()
        self._meta_cache = None

    # ---- rows / notes ---------------------------------------------------
    def hidden_rows(self, title: str, start_row: int, end_row: int) -> set[int]:
        """Returns 1-based row numbers in [start_row, end_row] hidden by the user."""
        res = self.svc.spreadsheets().get(
            spreadsheetId=self.sid,
            ranges=[f"'{title}'!A{start_row}:A{end_row}"],
            includeGridData=True,
            fields="sheets(data(startRow,rowMetadata(hiddenByUser)))",
        ).execute()
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
                    "range": {"sheetId": sid, "dimension": "ROWS", "startIndex": row - 1, "endIndex": row},
                    "properties": {"hiddenByUser": hidden},
                    "fields": "hiddenByUser",
                }
            }
            for row, hidden in changes
        ]
        self.svc.spreadsheets().batchUpdate(spreadsheetId=self.sid, body={"requests": reqs}).execute()

    def set_cell_note(self, title: str, row: int, col_letter: str, note: str):
        """Adds/overwrites a cell note at the given 1-based row + column letter."""
        sid = self.sheet_id(title)
        col = 0
        for ch in col_letter.upper():
            col = col * 26 + (ord(ch) - 64)
        col -= 1
        self.svc.spreadsheets().batchUpdate(
            spreadsheetId=self.sid,
            body={"requests": [{"updateCells": {
                "range": {"sheetId": sid, "startRowIndex": row - 1, "endRowIndex": row,
                          "startColumnIndex": col, "endColumnIndex": col + 1},
                "rows": [{"values": [{"note": note}]}],
                "fields": "note"}}]},
        ).execute()
