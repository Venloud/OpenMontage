"""Google Sheets client — read via CSV export (public) or gspread (service account).

Setup for write-back (updating STATUS / LINK FILE):
  1. Google Cloud Console → tạo Service Account → download JSON credentials
  2. Share sheet với service account email (Editor role)
  3. Set GOOGLE_SHEETS_CREDENTIALS=path/to/credentials.json trong .env

Without credentials, sheet is read-only via public CSV export.
"""
from __future__ import annotations

import csv
import io
import os
import re
import time
from pathlib import Path
from typing import Any

import requests


# ── URL parsing ───────────────────────────────────────────────────────────────

def parse_sheet_url(url_or_id: str) -> tuple[str, str]:
    """Return (spreadsheet_id, gid) from a Sheets URL or bare ID."""
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", url_or_id)
    sheet_id = m.group(1) if m else url_or_id
    m2 = re.search(r"gid=(\d+)", url_or_id)
    gid = m2.group(1) if m2 else "0"
    return sheet_id, gid


# ── CSV read (no auth, public sheets only) ───────────────────────────────────

def read_csv(url_or_id: str) -> list[list[str]]:
    """Fetch sheet as CSV. Works for public sheets without auth."""
    sheet_id, gid = parse_sheet_url(url_or_id)
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{sheet_id}"
        f"/export?format=csv&gid={gid}"
    )
    resp = requests.get(csv_url, timeout=30)
    resp.raise_for_status()
    reader = csv.reader(io.StringIO(resp.text))
    return list(reader)


def get_sheet_title(url_or_id: str) -> str:
    """Try to get the sheet/tab title. Falls back to gid."""
    sheet_id, gid = parse_sheet_url(url_or_id)
    try:
        # Use the Sheets API metadata endpoint (public metadata)
        meta_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        # Just use gid as fallback name
    except Exception:
        pass
    return gid


# ── gspread client (auth, read + write) ──────────────────────────────────────

def _gspread_client():
    """Return authenticated gspread client or None if not configured."""
    creds_path = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "credentials.json")
    if not Path(creds_path).exists():
        return None
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore

        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        return gspread.authorize(creds)
    except Exception:
        return None


def update_row(
    url_or_id: str,
    row_num: int,
    col_link: str,
    col_updated: str,
    col_status: str,
    link_value: str,
    status_value: str,
) -> bool:
    """Update LINK FILE, LATEST UPDATE, STATUS columns for a row.

    col_link / col_updated / col_status: column letter, e.g. "C", "D", "E"
    row_num: 1-based row number in the sheet.

    Returns True if write succeeded, False if no auth or error.
    """
    client = _gspread_client()
    if not client:
        return False
    sheet_id, gid = parse_sheet_url(url_or_id)
    try:
        sh = client.open_by_key(sheet_id)
        ws = sh.get_worksheet_by_id(int(gid))
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ws.batch_update([
            {"range": f"{col_link}{row_num}", "values": [[link_value]]},
            {"range": f"{col_updated}{row_num}", "values": [[ts]]},
            {"range": f"{col_status}{row_num}", "values": [[status_value]]},
        ])
        return True
    except Exception:
        return False


# ── Sheet rows → prompt list ──────────────────────────────────────────────────

def extract_prompts(
    rows: list[list[str]],
    prompt_col: int = 1,   # 0-indexed; default B = 1
    header_rows: int = 1,  # skip first N rows
) -> list[dict[str, Any]]:
    """Convert raw CSV rows into a list of prompt dicts.

    Returns: [{"row_num": 2, "prompt": "...", "raw_row": [...]}]
    Row nums are 1-based (matching Google Sheets row numbers).
    """
    result = []
    for i, row in enumerate(rows[header_rows:], start=header_rows + 1):
        if len(row) <= prompt_col:
            continue
        prompt = row[prompt_col].strip()
        if not prompt:
            continue
        result.append({"row_num": i, "prompt": prompt, "raw_row": row})
    return result
