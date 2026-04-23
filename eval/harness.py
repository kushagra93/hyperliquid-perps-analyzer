#!/usr/bin/env python3
"""
eval/harness.py
────────────────────────────────────────────────────────────────────
Weekly eval harness for Agent 3's causality verdicts.

Two subcommands:

  sample    Pull up to N recent alerts from Google Sheets (or a local
            JSONL alerts log if Sheets creds aren't available) and
            write `eval/to_label.csv` with blank human-label columns.

  score     Read a filled `eval/labeled.csv` and compute agreement
            between Agent 3's `primary_driver` and the human-labeled
            `correct_driver`. Prints overall accuracy, per-driver
            accuracy, and any systematic mis-classifications.

Intended as a weekly ritual: `sample` → label 20 rows in a spreadsheet
→ `score` → paste the accuracy into the team doc. When accuracy drops
below 0.8, tighten the prompt or rotate the model.

CSV schema (to_label.csv):
  timestamp, ticker, condition_id, agent3_driver, confidence,
  agent3_verdict, news_snippet, correct_driver, correct_confidence, notes

Valid `correct_driver` values: news, oi_flow, volume, technical, unknown
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logger = logging.getLogger(__name__)

VALID_DRIVERS = {"news", "oi_flow", "volume", "technical", "unknown"}
FIELDS = [
    "timestamp", "ticker", "condition_id", "agent3_driver", "confidence",
    "agent3_verdict", "news_snippet", "correct_driver",
    "correct_confidence", "notes",
]


def _load_from_sheets(limit: int) -> list[dict]:
    """Best-effort Sheets pull. Returns [] if creds or library missing."""
    try:
        import gspread  # type: ignore
        from google.oauth2.service_account import Credentials  # type: ignore
    except Exception:
        logger.info("gspread not installed — skipping Sheets pull.")
        return []

    creds_path = os.environ.get("GOOGLE_CREDENTIALS_FILE")
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not creds_path or not sheet_id:
        logger.info("GOOGLE_CREDENTIALS_FILE / GOOGLE_SHEET_ID not set — skipping Sheets.")
        return []
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets",
                  "https://www.googleapis.com/auth/drive"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scopes)
        client = gspread.authorize(creds)
        book = client.open_by_key(sheet_id)
        rows: list[dict] = []
        for ws in book.worksheets():
            for r in ws.get_all_records():
                rows.append(r)
        rows.sort(key=lambda r: str(r.get("Timestamp") or r.get("timestamp") or ""), reverse=True)
        return rows[:limit * 4]  # oversample; we'll sample below
    except Exception as e:
        logger.warning(f"Sheets pull failed: {e}")
        return []


def _load_from_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    return rows[-limit * 4:]


def _normalize(row: dict) -> dict:
    """Map Sheets-style and JSONL-style rows into a common shape."""
    get = lambda *keys: next((row[k] for k in keys if k in row and row[k] != ""), "")
    verdict = get("Verdict", "verdict", "agent3_verdict")
    news = get("News Summary", "news_summary", "news_snippet", "summary")
    return {
        "timestamp": get("Timestamp", "timestamp"),
        "ticker": get("Ticker", "ticker", "symbol"),
        "condition_id": get("Condition", "condition_id"),
        "agent3_driver": get("Primary Driver", "primary_driver", "agent3_driver"),
        "confidence": get("Confidence", "confidence"),
        "agent3_verdict": str(verdict)[:220],
        "news_snippet": str(news)[:220],
        "correct_driver": "",
        "correct_confidence": "",
        "notes": "",
    }


def cmd_sample(args) -> None:
    rows = _load_from_sheets(args.n)
    if not rows:
        rows = _load_from_jsonl(Path(args.fallback), args.n)
    if not rows:
        print("No alerts found in Sheets or JSONL. Exiting.", file=sys.stderr)
        sys.exit(1)

    normalized = [_normalize(r) for r in rows]
    normalized = [r for r in normalized if r["ticker"]]  # drop empty
    sampled = random.sample(normalized, min(args.n, len(normalized)))

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sampled)
    print(f"✓ Wrote {len(sampled)} rows to {out}")
    print("  Open in Sheets/Excel, fill `correct_driver` column, then run:")
    print(f"    python3 eval/harness.py score --in {out}")


def cmd_score(args) -> None:
    rows = list(csv.DictReader(Path(args.inp).open()))
    labeled = [r for r in rows if r.get("correct_driver", "").strip() in VALID_DRIVERS]
    if not labeled:
        print("No labeled rows found. Fill `correct_driver` column first.", file=sys.stderr)
        sys.exit(1)

    agree = sum(1 for r in labeled if r["agent3_driver"] == r["correct_driver"])
    total = len(labeled)
    per_driver_total: Counter = Counter()
    per_driver_right: Counter = Counter()
    confusion: dict = defaultdict(Counter)
    for r in labeled:
        t = r["correct_driver"]; p = r["agent3_driver"]
        per_driver_total[t] += 1
        if p == t: per_driver_right[t] += 1
        confusion[t][p] += 1

    print(f"\n=== Agent 3 eval — {total} labeled alerts ===")
    print(f"Overall accuracy: {agree}/{total} = {agree/total*100:.1f}%\n")
    print(f"{'true_driver':15s} {'n':>4s} {'acc':>7s}")
    for d in sorted(per_driver_total):
        n = per_driver_total[d]
        acc = per_driver_right[d] / n * 100
        print(f"{d:15s} {n:4d} {acc:6.1f}%")
    print("\nConfusion (rows = true, cols = predicted):")
    drivers = sorted(VALID_DRIVERS)
    print(f"{'':12s}" + "".join(f"{d:>11s}" for d in drivers))
    for t in drivers:
        print(f"{t:12s}" + "".join(f"{confusion[t][p]:11d}" for p in drivers))
    if agree / total < 0.8:
        print("\n⚠️  Accuracy below 0.8 threshold — consider tightening prompts or rotating model.")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sample", help="write to_label.csv")
    s.add_argument("-n", type=int, default=20, help="rows to sample")
    s.add_argument("--out", default="eval/to_label.csv")
    s.add_argument("--fallback", default="eval/alerts.jsonl",
                   help="local JSONL log fallback when Sheets unavailable")
    s.set_defaults(func=cmd_sample)

    c = sub.add_parser("score", help="compute accuracy from labeled.csv")
    c.add_argument("--in", dest="inp", default="eval/labeled.csv")
    c.set_defaults(func=cmd_score)

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
