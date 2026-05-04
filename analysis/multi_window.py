#!/usr/bin/env python3
"""
analysis/multi_window.py
────────────────────────────────────────────────────────────────────
Run the strategy across multiple lookback windows (30 / 60 / 90 / 120
/ 180 days) to test stability of the agent-fix findings.

The single biggest risk-warning from round-2 agents was sample size:
"60 days = 64 signals = statistical theatre". This script answers
the obvious next question — does the time-gate edge hold up over
3, 6 months as well, or is it window-specific?

Implementation:
  1. Fetch 180d of 15m candles per ticker (chunked to fit HL's
     ~5000-bar limit). This is one batch of HL hits, reused across
     all target windows.
  2. For each ticker, compute ATR + detect threshold breaches over
     the full 180d, then resolve TP1/SL/timeout walking forward.
  3. For each target window N ∈ {30, 60, 90, 120, 180}:
       a. Filter signals to those that fired within the last N days
       b. Compute v1 baseline (chase, no filter)
       c. Compute time-gate-only stats (the proven filter)
       d. Compute v3-stack stats (all filters + cluster cap)
  4. Tabulate side-by-side; push to Telegram.

If the time gate's edge is real, it should hold across windows. If
it's a 60d artifact, longer windows will show degradation.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.tickers import TICKERS
from analysis.historical_report import _atr, _score, _stars, MEGA, MEME
from analysis.system_v2 import (
    TICKER_CLUSTER, _hour_of, _size_bucket, build_lookup, score_v2,
    HOUR_OK, MAX_MOVE_PCT, _within_blackout, _cluster_dedupe,
)

IST = timezone(timedelta(hours=5, minutes=30))
HL = "https://api.hyperliquid.xyz/info"
BAR_MS = 15 * 60 * 1000
CHUNK_BARS = 1900   # ~20 days at 15m; HL times out near the 5000-bar cap


# ── Chunked candle fetch ─────────────────────────────────────────

def fetch_chunked(coin: str, days: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    start = now_ms - days * 24 * 3600 * 1000
    out: list[dict] = []
    cursor = start
    span = CHUNK_BARS * BAR_MS
    while cursor < now_ms:
        end = min(cursor + span, now_ms)
        payload = {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "15m",
            "startTime": cursor, "endTime": end,
        }}
        try:
            r = subprocess.run(
                ["curl", "-s", "-X", "POST", HL,
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(payload)],
                capture_output=True, text=True, timeout=45,
            )
            arr = json.loads(r.stdout) or []
        except Exception:
            arr = []
        if not isinstance(arr, list) or not arr:
            # HL has no data for this chunk window (likely too far back).
            # Skip ahead instead of bailing — older xyz: tickers only
            # have ~30-90 days of history.
            cursor = end + 1
            time.sleep(0.4)
            continue
        out.extend(arr)
        cursor = int(arr[-1]["T"]) + 1
        time.sleep(0.4)  # be polite to HL while live watcher also hits it
    # de-dup chunk overlaps
    seen = set(); dedup = []
    for c in out:
        if c["t"] in seen: continue
        seen.add(c["t"]); dedup.append(c)
    dedup.sort(key=lambda c: int(c["t"]))
    return dedup


# ── Detect & resolve (mirrors historical_report logic) ───────────

def detect_signals(sym: str, full: str, candles: list[dict], threshold_pct: float) -> list[dict]:
    if len(candles) < 30:
        return []
    a14 = _atr(candles, 14) or 0.0   # _atr expects dict-style HL candles
    sigs: list[dict] = []
    tier = "mega" if sym in MEGA else ("meme" if sym in MEME else "other")
    for i in range(1, len(candles)):
        prev_c = float(candles[i - 1]["c"])
        cur_c = float(candles[i]["c"])
        if prev_c <= 0: continue
        m = (cur_c - prev_c) / prev_c * 100
        if abs(m) < threshold_pct:
            continue
        ts = int(candles[i]["t"])
        direction = "up" if m > 0 else "down"
        score = _score(direction, sym, m)
        sigs.append({
            "ts_ms": ts,
            "when_ist": datetime.fromtimestamp(ts/1000, tz=IST).isoformat(timespec="minutes"),
            "ticker": sym, "full_name": full,
            "direction": direction,
            "condition_id": "C1" if direction == "up" else "C2",
            "move_pct": round(m, 2), "threshold_pct": threshold_pct,
            "price": cur_c, "atr": round(a14, 4),
            "score": score, "stars": _stars(score), "tier": tier,
            "outcome": "open", "pnl_pct": None,
            "bars_to_resolution": None, "exit_price": None,
        })
    return sigs


def resolve(sig: dict, candles: list[dict], max_bars: int = 24) -> dict:
    a = sig["atr"] or sig["price"] * 0.01
    direction = sig["direction"]
    entry = sig["price"]
    sl = entry - 1.5 * a if direction == "up" else entry + 1.5 * a
    tp = entry + 2.0 * a if direction == "up" else entry - 2.0 * a
    # find candle index
    start = None
    for i, c in enumerate(candles):
        if int(c["t"]) == sig["ts_ms"]:
            start = i; break
    if start is None or start + 1 >= len(candles):
        return sig
    for j in range(start + 1, min(start + 1 + max_bars, len(candles))):
        c = candles[j]
        hi, lo = float(c["h"]), float(c["l"])
        bars = j - start
        if direction == "up":
            if lo <= sl:
                sig["outcome"] = "sl"; sig["exit_price"] = sl
                sig["pnl_pct"] = round((sl - entry) / entry * 100, 3)
                sig["bars_to_resolution"] = bars; return sig
            if hi >= tp:
                sig["outcome"] = "tp1"; sig["exit_price"] = tp
                sig["pnl_pct"] = round((tp - entry) / entry * 100, 3)
                sig["bars_to_resolution"] = bars; return sig
        else:
            if hi >= sl:
                sig["outcome"] = "sl"; sig["exit_price"] = sl
                sig["pnl_pct"] = round((entry - sl) / entry * 100, 3)
                sig["bars_to_resolution"] = bars; return sig
            if lo <= tp:
                sig["outcome"] = "tp1"; sig["exit_price"] = tp
                sig["pnl_pct"] = round((entry - tp) / entry * 100, 3)
                sig["bars_to_resolution"] = bars; return sig
    last = candles[min(start + max_bars, len(candles) - 1)]
    exit_p = float(last["c"])
    pnl = (exit_p - entry) / entry * 100 if direction == "up" else (entry - exit_p) / entry * 100
    sig["outcome"] = "timeout"; sig["exit_price"] = exit_p
    sig["pnl_pct"] = round(pnl, 3); sig["bars_to_resolution"] = max_bars
    return sig


# ── Window stats ─────────────────────────────────────────────────

def stats(sigs: list[dict]) -> dict:
    closed = [s for s in sigs if s.get("outcome") in ("tp1", "sl", "timeout")]
    if not closed:
        return {"n": 0, "wins": 0, "win_rate_pct": 0.0,
                "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0}
    wins = sum(1 for s in closed if (s.get("pnl_pct") or 0) > 0)
    total = sum((s.get("pnl_pct") or 0) for s in closed)
    return {
        "n": len(closed), "wins": wins,
        "win_rate_pct": round(wins / len(closed) * 100, 1),
        "total_pnl_pct": round(total, 2),
        "avg_pnl_pct": round(total / len(closed), 3),
    }


def time_gate(sigs: list[dict]) -> list[dict]:
    return [s for s in sigs if HOUR_OK[0] <= _hour_of(s["when_ist"]) <= HOUR_OK[1]]


def v3_stack(sigs: list[dict]) -> list[dict]:
    closed = [s for s in sigs if s.get("outcome") in ("tp1", "sl", "timeout")]
    lookup = build_lookup(closed)
    filtered = []
    for s in closed:
        if not (HOUR_OK[0] <= _hour_of(s["when_ist"]) <= HOUR_OK[1]):
            continue
        if abs(s.get("move_pct") or 0) > MAX_MOVE_PCT:
            continue
        if score_v2(s, lookup) < 50:
            continue
        if _within_blackout(s, 4):
            continue
        filtered.append(s)
    return _cluster_dedupe(filtered)


# ── Driver ───────────────────────────────────────────────────────

WINDOWS = [30, 60, 90, 120, 180]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--days-max", type=int, default=180)
    args = p.parse_args()

    print(f"Fetching {args.days_max}d of 15m candles for {len(TICKERS)} tickers (chunked)…", flush=True)
    all_signals: list[dict] = []
    for sym, cfg in TICKERS.items():
        coin = cfg["hl_asset"]
        full = cfg.get("full_name", sym)
        threshold = cfg["price_change_threshold_pct"]
        t0 = time.time()
        candles = fetch_chunked(coin, args.days_max)
        elapsed = time.time() - t0
        print(f"    [debug] {sym}: fetch_chunked returned {len(candles)} in {elapsed:.2f}s", flush=True)
        if len(candles) < 30:
            print(f"  {sym}: insufficient ({len(candles)} bars)")
            continue
        sigs = detect_signals(sym, full, candles, threshold)
        for s in sigs:
            resolve(s, candles)
        all_signals.extend(sigs)
        print(f"  {sym}: {len(candles)} bars · {len(sigs)} signals")

    print(f"\nTotal signals across 180d: {len(all_signals)}")

    # Per-window slicing
    now_ms = int(time.time() * 1000)
    by_window: dict[int, dict] = {}
    for N in WINDOWS:
        cutoff = now_ms - N * 24 * 3600 * 1000
        window_sigs = [s for s in all_signals if s["ts_ms"] >= cutoff]
        baseline = stats(window_sigs)
        time_only = stats(time_gate(window_sigs))
        v3 = stats(v3_stack(window_sigs))
        by_window[N] = {
            "baseline": baseline,
            "time_gate_only": time_only,
            "v3_stack": v3,
            "n_in_window": len(window_sigs),
        }
        print(f"\n[{N:>3}d]  baseline n={baseline['n']:3d} wr={baseline['win_rate_pct']:5.1f}% pnl={baseline['total_pnl_pct']:+7.2f}%"
              f"   time-gate n={time_only['n']:3d} wr={time_only['win_rate_pct']:5.1f}% pnl={time_only['total_pnl_pct']:+7.2f}%"
              f"   v3-stack n={v3['n']:3d} wr={v3['win_rate_pct']:5.1f}% pnl={v3['total_pnl_pct']:+7.2f}%")

    out = {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "windows_days": WINDOWS,
        "by_window": by_window,
    }
    (ROOT / "analysis" / "multi_window_result.json").write_text(json.dumps(out, indent=2))

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        def tg(t):
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat}",
                "--data-urlencode", f"text={t}",
                "--data-urlencode", "parse_mode=HTML",
                "--data-urlencode", "disable_web_page_preview=true",
            ], capture_output=True, text=True, timeout=15)

        head = ("<b>📊 MULTI-WINDOW BACKTEST</b>\n"
                "<i>Stability check across 30 / 60 / 90 / 120 / 180-day windows. "
                "If the time-gate edge is real, it should hold up. If it was a "
                "60d artifact, longer windows will show degradation.</i>")
        tg(head); time.sleep(0.4)

        # Baseline table
        lines = ["<b>Baseline (chase, no filter)</b>", "<pre>", f"{'win':>3s} {'n':>4s} {'wr%':>6s} {'PnL%':>8s}"]
        for N in WINDOWS:
            r = by_window[N]["baseline"]
            lines.append(f"{N:>3d}d {r['n']:>4d} {r['win_rate_pct']:>5.1f}% {r['total_pnl_pct']:>+7.2f}%")
        lines.append("</pre>")
        tg("\n".join(lines)); time.sleep(0.4)

        # Time-gate table
        lines = ["<b>Time gate ONLY (19-22 IST)</b>", "<pre>", f"{'win':>3s} {'n':>4s} {'wr%':>6s} {'PnL%':>8s}"]
        for N in WINDOWS:
            r = by_window[N]["time_gate_only"]
            lines.append(f"{N:>3d}d {r['n']:>4d} {r['win_rate_pct']:>5.1f}% {r['total_pnl_pct']:>+7.2f}%")
        lines.append("</pre>")
        tg("\n".join(lines)); time.sleep(0.4)

        # v3-stack table
        lines = ["<b>v3 stack (time + size + score + blackout + cluster)</b>", "<pre>", f"{'win':>3s} {'n':>4s} {'wr%':>6s} {'PnL%':>8s}"]
        for N in WINDOWS:
            r = by_window[N]["v3_stack"]
            lines.append(f"{N:>3d}d {r['n']:>4d} {r['win_rate_pct']:>5.1f}% {r['total_pnl_pct']:>+7.2f}%")
        lines.append("</pre>")
        tg("\n".join(lines)); time.sleep(0.4)

        # Verdict
        time_60 = by_window[60]["time_gate_only"]
        time_180 = by_window[180]["time_gate_only"]
        verdict = (
            "<b>📋 VERDICT</b>\n\n"
            f"Time-gate stability: {time_60['win_rate_pct']}% wr at 60d → "
            f"{time_180['win_rate_pct']}% wr at 180d.\n"
            f"PnL: {time_60['total_pnl_pct']:+}% at 60d → "
            f"{time_180['total_pnl_pct']:+}% at 180d.\n\n"
            f"Sample size grows ~3× from 60d to 180d, which dramatically "
            f"tightens the bootstrap CI. If the WR drift is small, the "
            f"time-gate edge is structural (regime-independent). If it "
            f"drifts hard, the 60d result was window-specific.\n\n"
            f"<i>Bigger sample → tighter CI → real conclusions possible.</i>"
        )
        tg(verdict)


if __name__ == "__main__":
    main()
