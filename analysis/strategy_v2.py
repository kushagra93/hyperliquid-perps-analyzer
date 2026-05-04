#!/usr/bin/env python3
"""
analysis/strategy_v2.py
────────────────────────────────────────────────────────────────────
Strategy v2 — fade the spike, gated by time + size, with tighter
TP and wider SL. Built from RCA on the 60-day historical corpus.

Diagnostic findings that motivate v2:
  • Original strategy (chase the move) → 25% win rate
  • Sign-flip (fade) → 75% win rate
  • 47/48 stop-losses hit within 2 bars (capitulation reversal)
  • US cash hours (19:00–22:00 IST) → 50%+ win rate
  • Off-hours (01:00, 03:00, 16:00 IST) → ≤10% win rate
  • Move size ≤ 2% → 53% win rate; 3-5% → 21%; 5%+ → 0%

v2 design:
  Side:    FADE (opposite of price direction)
  Entry:   next bar open after detection
  SL:      ±2.0 × ATR (wider — give the fade room)
  TP1:     ±1.0 × ATR (tighter — book before mean-reversion fades)
  Time:    max 8 bars (~2h)
  Filters: hour ∈ [19,22] IST · |move| ≤ 3.0% · drop chop tickers

This script:
  1. Loads existing analysis/report.json (signals + HL price/atr)
  2. For each signal that passes v2 filters, re-walks forward HL
     candles with v2 TP/SL/time params under FADE side
  3. Compares aggregate vs the original strategy
  4. Pushes a Telegram thread with the comparison
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

IST = timezone(timedelta(hours=5, minutes=30))
HL = "https://api.hyperliquid.xyz/info"

# ── Strategy v2 parameters ───────────────────────────────────────
HOUR_OK = (19, 22)        # IST window (inclusive)
MAX_MOVE_PCT = 3.0        # filter capitulations
TP_ATR_MULT = 1.0
SL_ATR_MULT = 2.0
MAX_BARS = 8
DROP_TICKERS: set[str] = set()  # disabled by default; tune if needed


def _candles(coin: str, ts_ms_start: int, ts_ms_end: int) -> list[dict]:
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "15m",
        "startTime": ts_ms_start, "endTime": ts_ms_end,
    }}
    for attempt in (1, 2):
        try:
            r = subprocess.run(
                ["curl", "-s", "-X", "POST", HL,
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload)],
                capture_output=True, text=True,
                timeout=30 if attempt == 1 else 60,
            )
            return json.loads(r.stdout) or []
        except subprocess.TimeoutExpired:
            if attempt == 2:
                return []
            time.sleep(1)
        except Exception:
            return []
    return []


def _ist_hour(when_iso: str) -> int:
    return datetime.fromisoformat(when_iso).hour


def _passes_v2_filter(s: dict) -> tuple[bool, str]:
    h = _ist_hour(s["when_ist"])
    if not (HOUR_OK[0] <= h <= HOUR_OK[1]):
        return False, f"hour {h:02d} outside {HOUR_OK[0]:02d}–{HOUR_OK[1]:02d}"
    if abs(s.get("move_pct") or 0) > MAX_MOVE_PCT:
        return False, f"move {s['move_pct']:+.2f}% exceeds {MAX_MOVE_PCT}%"
    if s["ticker"] in DROP_TICKERS:
        return False, f"ticker {s['ticker']} in drop list"
    return True, ""


def _resolve_v2(s: dict) -> dict:
    """Re-walk forward HL candles under v2 (fade) side + tighter TP/wider SL."""
    coin = f"xyz:{s['ticker']}"
    entry_ts = s["ts_ms"]
    entry = float(s["price"])
    atr = float(s.get("atr") or 0) or entry * 0.01

    # v2 side = OPPOSITE of original price direction
    fade_long = (s["direction"] == "down")  # price was down → fade by going LONG
    if fade_long:
        sl = entry - SL_ATR_MULT * atr
        tp = entry + TP_ATR_MULT * atr
    else:
        sl = entry + SL_ATR_MULT * atr
        tp = entry - TP_ATR_MULT * atr

    # Fetch the next ~MAX_BARS+2 candles after entry
    end_ts = entry_ts + (MAX_BARS + 2) * 15 * 60 * 1000
    cs = _candles(coin, entry_ts, end_ts)
    forward = [c for c in cs if int(c["t"]) > entry_ts][:MAX_BARS]
    if not forward:
        return {**s, "v2_outcome": "no_data", "v2_pnl_pct": None,
                "v2_bars": None, "v2_side": "long" if fade_long else "short"}

    for j, c in enumerate(forward, 1):
        hi = float(c["h"]); lo = float(c["l"])
        if fade_long:
            if lo <= sl: return _close(s, fade_long, entry, sl, j, "sl")
            if hi >= tp: return _close(s, fade_long, entry, tp, j, "tp1")
        else:
            if hi >= sl: return _close(s, fade_long, entry, sl, j, "sl")
            if lo <= tp: return _close(s, fade_long, entry, tp, j, "tp1")

    # Time stop — close at last close
    last = forward[-1]
    exit_p = float(last["c"])
    return _close(s, fade_long, entry, exit_p, len(forward), "timeout")


def _close(s, fade_long, entry, exit_p, bars, outcome):
    pnl = (exit_p - entry) / entry * 100 if fade_long else (entry - exit_p) / entry * 100
    return {**s, "v2_outcome": outcome, "v2_pnl_pct": round(pnl, 3),
            "v2_bars": bars, "v2_side": "long" if fade_long else "short",
            "v2_exit": exit_p}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--report", default="analysis/report.json")
    p.add_argument("--telegram", action="store_true")
    args = p.parse_args()

    rep = json.loads((ROOT / args.report).read_text())
    sigs = rep["signals"]
    closed = [s for s in sigs if s.get("outcome") in ("tp1", "sl", "timeout")]

    # Apply filters
    filtered = []
    skipped = []
    for s in closed:
        ok, reason = _passes_v2_filter(s)
        if ok:
            filtered.append(s)
        else:
            skipped.append((s, reason))

    print(f"Original closed: {len(closed)} · v2-eligible: {len(filtered)} · skipped: {len(skipped)}\n")

    # Re-resolve under v2
    resolved = []
    for i, s in enumerate(filtered, 1):
        r = _resolve_v2(s)
        resolved.append(r)
        print(f"  [{i}/{len(filtered)}] {s['ticker']:5s}  v1={s['outcome']:7s} {s.get('pnl_pct',0):+.2f}%  →  "
              f"v2={r['v2_outcome']:7s} {r.get('v2_pnl_pct') or 0:+.2f}% ({r['v2_side']})")

    v2_closed = [r for r in resolved if r["v2_outcome"] in ("tp1", "sl", "timeout")]
    v2_wins = [r for r in v2_closed if (r.get("v2_pnl_pct") or 0) > 0]
    v2_pnl = sum((r.get("v2_pnl_pct") or 0) for r in v2_closed)
    v2_avg_win = (sum((r.get("v2_pnl_pct") or 0) for r in v2_wins) / len(v2_wins)) if v2_wins else 0.0
    v2_losses = [r for r in v2_closed if (r.get("v2_pnl_pct") or 0) <= 0]
    v2_avg_loss = (sum((r.get("v2_pnl_pct") or 0) for r in v2_losses) / len(v2_losses)) if v2_losses else 0.0

    # Original on the same SUBSET (so comparison is apples-to-apples)
    v1_subset_pnl = sum((s.get("pnl_pct") or 0) for s in filtered)
    v1_subset_wins = sum(1 for s in filtered if (s.get("pnl_pct") or 0) > 0)

    print("\n" + "═" * 60)
    print("STRATEGY v1 (chase) on this subset:")
    print(f"  n={len(filtered)}  wins={v1_subset_wins}  "
          f"wr={v1_subset_wins/len(filtered)*100:.1f}%  total PnL={v1_subset_pnl:+.2f}%")
    print(f"\nSTRATEGY v2 (fade + filters) on same subset:")
    print(f"  n={len(v2_closed)}  wins={len(v2_wins)}  "
          f"wr={len(v2_wins)/len(v2_closed)*100:.1f}%  total PnL={v2_pnl:+.2f}%")
    print(f"  avg win {v2_avg_win:+.2f}%  avg loss {v2_avg_loss:+.2f}%")

    # Save resolved data
    out = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "scan_days": rep["scan_days"],
        "params": {
            "hour_ok": list(HOUR_OK), "max_move_pct": MAX_MOVE_PCT,
            "tp_atr_mult": TP_ATR_MULT, "sl_atr_mult": SL_ATR_MULT,
            "max_bars": MAX_BARS, "drop_tickers": list(DROP_TICKERS),
            "side": "FADE (opposite of price direction)",
        },
        "v1_baseline_on_subset": {
            "n": len(filtered),
            "wins": v1_subset_wins,
            "win_rate_pct": round(v1_subset_wins / len(filtered) * 100, 1) if filtered else 0,
            "total_pnl_pct": round(v1_subset_pnl, 2),
        },
        "v2_results": {
            "n": len(v2_closed),
            "wins": len(v2_wins),
            "win_rate_pct": round(len(v2_wins) / len(v2_closed) * 100, 1) if v2_closed else 0,
            "total_pnl_pct": round(v2_pnl, 2),
            "avg_win_pct": round(v2_avg_win, 2),
            "avg_loss_pct": round(v2_avg_loss, 2),
            "expectancy_pct": round(
                (len(v2_wins) / len(v2_closed)) * v2_avg_win
                + (1 - len(v2_wins) / len(v2_closed)) * v2_avg_loss, 3
            ) if v2_closed else 0,
        },
        "trades": resolved,
        "skipped_count": len(skipped),
    }
    (ROOT / "analysis" / "strategy_v2_result.json").write_text(json.dumps(out, indent=2))
    print(f"\nWrote analysis/strategy_v2_result.json")

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        v1 = out["v1_baseline_on_subset"]; v2 = out["v2_results"]
        delta_wr = v2["win_rate_pct"] - v1["win_rate_pct"]
        delta_pnl = v2["total_pnl_pct"] - v1["total_pnl_pct"]

        msgs = [
            f"<b>🧪 STRATEGY v2 BACKTEST</b>\n"
            f"<i>Fade-the-spike + time gate (19-22 IST) + size cap (≤3%) "
            f"+ tighter TP (1×ATR) + wider SL (2×ATR), 8-bar time stop.</i>\n\n"
            f"<b>Same {v2['n']} signals, head-to-head:</b>",

            f"<b>v1 (chase) on this subset</b>\n"
            f"  • Win rate: {v1['win_rate_pct']}% ({v1['wins']}/{v1['n']})\n"
            f"  • Total PnL: <b>{v1['total_pnl_pct']:+.2f}%</b>",

            f"<b>v2 (fade + filters) on this subset</b>\n"
            f"  • Win rate: <b>{v2['win_rate_pct']}%</b> "
            f"({v2['wins']}/{v2['n']})  "
            f"<i>{'+'+str(round(delta_wr,1)) if delta_wr>=0 else round(delta_wr,1)} pts</i>\n"
            f"  • Total PnL: <b>{v2['total_pnl_pct']:+.2f}%</b>  "
            f"<i>{'+'+str(round(delta_pnl,2)) if delta_pnl>=0 else round(delta_pnl,2)} pts</i>\n"
            f"  • Avg win: {v2['avg_win_pct']:+.2f}%  ·  "
            f"avg loss: {v2['avg_loss_pct']:+.2f}%\n"
            f"  • Expectancy: <b>{v2['expectancy_pct']:+.3f}% per trade</b>",
        ]
        # Per-trade comparison
        lines = ["<b>Trade-by-trade comparison</b>", "<pre>",
                 f"{'Ticker':6s} {'when':>5s} {'v1':>7s} {'v2':>7s} {'side':>5s}"]
        for r in resolved[:25]:
            t = datetime.fromisoformat(r["when_ist"]).strftime("%m-%d")
            v1_pnl = r.get("pnl_pct"); v2_pnl = r.get("v2_pnl_pct")
            v1_s = f"{v1_pnl:+.2f}%" if v1_pnl is not None else "—"
            v2_s = f"{v2_pnl:+.2f}%" if v2_pnl is not None else "—"
            lines.append(f"{r['ticker']:6s} {t:>5s} {v1_s:>7s} {v2_s:>7s} {r['v2_side']:>5s}")
        lines.append("</pre>")
        msgs.append("\n".join(lines))

        msgs.append(
            "<b>Recommendation</b>\n"
            "If v2 lifts win rate by &gt;20pts and PnL turns positive, "
            "ship as the default playbook in <code>config/pn_filter.json</code>. "
            "The mechanism: when our system says LONG on a 3-5% spike, "
            "trader does a SHORT in 1-2h scalp. When it says SHORT, "
            "trader goes LONG. Same signal, opposite action.\n\n"
            "<i>Caveat: 60d sample is small. Refit weekly.</i>"
        )

        for m in msgs:
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat}",
                "--data-urlencode", f"text={m}",
                "--data-urlencode", "parse_mode=HTML",
                "--data-urlencode", "disable_web_page_preview=true",
            ], capture_output=True, text=True, timeout=15)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
