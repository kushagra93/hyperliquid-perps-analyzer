# notifiers/sheets.py
# ─────────────────────────────────────────────────────────────────
# Appends a structured alert row to a Google Sheet.
# Uses a service account JSON for auth (no OAuth flow needed).
#
# Required setup:
# 1. Create a Google Cloud project + enable Sheets API
# 2. Create a Service Account + download credentials.json
# 3. Share your Google Sheet with the service account email
# ─────────────────────────────────────────────────────────────────

import gspread
import logging
from datetime import datetime
from google.oauth2.service_account import Credentials
from config.settings import (
    GOOGLE_CREDENTIALS_FILE,
    GOOGLE_SHEET_ID,
    GOOGLE_SHEET_TAB,
)

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column headers — written once if the sheet is empty
HEADERS = [
    "Timestamp (UTC)",
    "Asset",
    "Current Price",
    "Price Δ%",
    "OI Δ%",
    "Condition",
    "Condition Label",
    "Primary Driver",
    "Confidence",
    "Verdict",
    "Flags",
    "News Summary",
    "Reasoning",
]


def _get_worksheet():
    creds = Credentials.from_service_account_file(GOOGLE_CREDENTIALS_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID)
    try:
        ws = sheet.worksheet(GOOGLE_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title=GOOGLE_SHEET_TAB, rows=1000, cols=20)
        logger.info(f"[Sheets] Created new worksheet: {GOOGLE_SHEET_TAB}")
    return ws


def _ensure_headers(ws):
    """Write header row if the sheet is empty."""
    existing = ws.row_values(1)
    if not existing:
        ws.append_row(HEADERS, value_input_option="RAW")
        logger.info("[Sheets] Header row written.")


def log_alert(
    price_trigger: dict,
    oi_report: dict,
    condition: dict,
    causality: dict,
    news_report: dict,
):
    """
    Appends one alert row to the configured Google Sheet.

    Columns: Timestamp | Asset | Price | Price Δ% | OI Δ% |
             Condition | Label | Driver | Confidence | Verdict |
             Flags | News Summary | Reasoning
    """
    try:
        ws = _get_worksheet()
        _ensure_headers(ws)

        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        flags_str = ", ".join(causality.get("flags", []))
        news_summary = news_report.get("summary", "")[:500]   # cap length

        row = [
            now,
            price_trigger["asset"],
            round(price_trigger["current_price"], 4),
            round(price_trigger["price_change_pct"], 3),
            round(oi_report["oi_change_pct"], 3),
            condition["condition_id"],
            condition["label"],
            causality.get("primary_driver", ""),
            causality.get("confidence", ""),
            causality.get("verdict", ""),
            flags_str,
            news_summary,
            causality.get("reasoning", ""),
        ]

        ws.append_row(row, value_input_option="USER_ENTERED")
        logger.info(f"[Sheets] Alert row appended: {condition['condition_id']} | {causality.get('verdict', '')[:60]}")

    except Exception as e:
        logger.error(f"[Sheets] Failed to write alert: {e}")
