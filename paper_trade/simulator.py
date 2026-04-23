#!/usr/bin/env python3
"""
paper_trade/simulator.py
────────────────────────────────────────────────────────────────────
Forward-going paper-trade simulator. Watches an alerts JSONL stream
(tailed or polled) and opens virtual positions, marks them to market
via HL candleSnapshot, and logs PnL.

Rules (simple, deterministic — mirrors the Telegram playbook):
  - C1 (price↑, OI↑) → long
  - C2 (price↓, OI↑) → short
  - C3/C4 → skip (too weak for autotrade)
  - Entry  = alert `current_price`
  - Stop   = entry ∓ 1.5 × ATR14 (ATR fetched from HL 15m candles)
  - TP1    = entry ± 2.0 × ATR14 (half size)
  - TP2    = entry ± 4.0 × ATR14 (second half)
  - Timeout = 24 × 15m bars (~6 hours)

Persistence:
  paper_trade/positions.csv  — one row per trade (entry, exit, pnl)
  paper_trade/summary.json   — running totals

Usage:
  python3 paper_trade/simulator.py --alerts eval/alerts.jsonl --poll 30

For a live pipeline, have ticker_worker append each alert to the
alerts JSONL file (one small addition — not wired yet in this patch).
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

HL = "https://api.hyperliquid.xyz/info"


def _hl_candles(coin: str, interval: str, lb_ms: int) -> list[tuple]:
    now = int(time.time() * 1000)
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": interval,
        "startTime": now - lb_ms, "endTime": now,
    }}
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", HL,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=15,
    )
    try:
        arr = json.loads(r.stdout)
        return [(int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]),
                 float(c["c"]), float(c["v"])) for c in arr]
    except Exception:
        return []


def _atr(cs: list[tuple], n: int = 14) -> float:
    if len(cs) < n + 1:
        return cs[-1][4] * 0.01 if cs else 0.0
    trs = []
    for i in range(1, len(cs)):
        h, l, pc = cs[i][2], cs[i][3], cs[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[:n]) / n
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
    return a


def _side_from_condition(cid: str) -> str | None:
    return {"C1": "long", "C2": "short"}.get(cid)


def open_position(alert: dict) -> dict | None:
    """Build a position dict from an alert."""
    cid = alert.get("condition", {}).get("condition_id") \
        or alert.get("condition_id")
    side = _side_from_condition(cid)
    if not side:
        return None

    sym = alert.get("symbol") or alert.get("ticker")
    if not sym:
        return None

    entry = float((alert.get("price_trigger") or {}).get("current_price")
                  or alert.get("current_price") or 0.0)
    if entry <= 0:
        return None

    coin = (alert.get("hl_asset")
            or alert.get("price_trigger", {}).get("asset")
            or f"xyz:{sym}")
    cs = _hl_candles(coin, "15m", 2 * 24 * 3600 * 1000)
    a = _atr(cs, 14) or entry * 0.01

    if side == "long":
        stop = entry - 1.5 * a
        tp1 = entry + 2.0 * a
        tp2 = entry + 4.0 * a
    else:
        stop = entry + 1.5 * a
        tp1 = entry - 2.0 * a
        tp2 = entry - 4.0 * a

    return {
        "id": f"{sym}-{cid}-{int(time.time())}",
        "ticker": sym, "coin": coin, "side": side, "condition": cid,
        "entry_ts": int(time.time() * 1000),
        "entry": entry, "atr": a,
        "stop": stop, "tp1": tp1, "tp2": tp2,
        "status": "open", "exit": None, "exit_ts": None, "pnl_pct": None,
        "exit_reason": None,
    }


def _pnl_pct(pos: dict, exit_price: float) -> float:
    if pos["side"] == "long":
        return (exit_price - pos["entry"]) / pos["entry"] * 100
    return (pos["entry"] - exit_price) / pos["entry"] * 100


def mark_to_market(pos: dict, max_bars: int = 24) -> dict:
    """Walk 15m candles from entry_ts forward; resolve to stop/tp/timeout."""
    if pos["status"] != "open":
        return pos
    lookback_ms = max_bars * 15 * 60 * 1000 + 60_000
    cs = _hl_candles(pos["coin"], "15m", lookback_ms)
    cs = [c for c in cs if c[0] >= pos["entry_ts"]]
    cs = cs[:max_bars]
    if not cs:
        return pos  # no bars yet; still open

    for c in cs:
        hi, lo = c[2], c[3]
        if pos["side"] == "long":
            if lo <= pos["stop"]:
                pos.update(status="closed", exit=pos["stop"], exit_ts=c[0],
                           exit_reason="stop", pnl_pct=_pnl_pct(pos, pos["stop"]))
                return pos
            if hi >= pos["tp1"]:
                pos.update(status="closed", exit=pos["tp1"], exit_ts=c[0],
                           exit_reason="tp1", pnl_pct=_pnl_pct(pos, pos["tp1"]))
                return pos
        else:
            if hi >= pos["stop"]:
                pos.update(status="closed", exit=pos["stop"], exit_ts=c[0],
                           exit_reason="stop", pnl_pct=_pnl_pct(pos, pos["stop"]))
                return pos
            if lo <= pos["tp1"]:
                pos.update(status="closed", exit=pos["tp1"], exit_ts=c[0],
                           exit_reason="tp1", pnl_pct=_pnl_pct(pos, pos["tp1"]))
                return pos

    # Timeout — close at last candle close
    last = cs[-1]
    if len(cs) >= max_bars:
        pos.update(status="closed", exit=last[4], exit_ts=last[0],
                   exit_reason="timeout", pnl_pct=_pnl_pct(pos, last[4]))
    return pos


# ─── Persistence ──────────────────────────────────────────────────

POS_HEADERS = ["id", "ticker", "coin", "side", "condition",
               "entry_ts", "entry", "atr", "stop", "tp1", "tp2",
               "status", "exit", "exit_ts", "pnl_pct", "exit_reason"]


def _load_positions(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _save_positions(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POS_HEADERS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in POS_HEADERS})


def _write_summary(path: Path, rows: list[dict]) -> None:
    closed = [r for r in rows if r.get("status") == "closed"]
    pnls = [float(r["pnl_pct"]) for r in closed if r.get("pnl_pct") not in (None, "", "None")]
    wins = [p for p in pnls if p > 0]
    summary = {
        "total_positions": len(rows),
        "closed": len(closed),
        "open": len(rows) - len(closed),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 2) if pnls else 0.0,
        "avg_pnl_pct": round(sum(pnls) / len(pnls), 3) if pnls else 0.0,
        "total_pnl_pct": round(sum(pnls), 2),
    }
    path.write_text(json.dumps(summary, indent=2))


# ─── Main loop ────────────────────────────────────────────────────

def run(alerts_path: Path, positions_path: Path, summary_path: Path, poll_sec: int) -> None:
    seen_ids: set[str] = set()
    positions = _load_positions(positions_path)
    for p in positions:
        seen_ids.add(p["id"])
    logger.info(f"Resumed with {len(positions)} existing positions.")

    while True:
        # 1) Ingest new alerts
        if alerts_path.exists():
            for line in alerts_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    alert = json.loads(line)
                except Exception:
                    continue
                pos = open_position(alert)
                if not pos:
                    continue
                if pos["id"] in seen_ids:
                    continue
                positions.append({k: pos[k] for k in POS_HEADERS})
                seen_ids.add(pos["id"])
                logger.info(f"Opened {pos['side']} {pos['ticker']} @ {pos['entry']:.2f}")

        # 2) Mark open positions to market
        for r in positions:
            if r.get("status") != "open":
                continue
            # Coerce string values back to numeric for math
            num = dict(r)
            for k in ("entry_ts", "entry", "atr", "stop", "tp1", "tp2"):
                if num.get(k) not in (None, ""):
                    try:
                        num[k] = int(num[k]) if k == "entry_ts" else float(num[k])
                    except Exception:
                        pass
            resolved = mark_to_market(num)
            for k in POS_HEADERS:
                r[k] = resolved.get(k, r.get(k, ""))
            if resolved.get("status") == "closed":
                logger.info(
                    f"Closed {r['ticker']} {r['side']} via {r['exit_reason']} → "
                    f"{r.get('pnl_pct', 0):.2f}%"
                )

        _save_positions(positions_path, positions)
        _write_summary(summary_path, positions)

        if poll_sec <= 0:
            break
        time.sleep(poll_sec)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--alerts", default="eval/alerts.jsonl",
                   help="JSONL of alert dicts (one per line)")
    p.add_argument("--positions", default="paper_trade/positions.csv")
    p.add_argument("--summary", default="paper_trade/summary.json")
    p.add_argument("--poll", type=int, default=30,
                   help="seconds between polls; 0 = single pass")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
    run(Path(args.alerts), Path(args.positions), Path(args.summary), args.poll)


if __name__ == "__main__":
    main()
