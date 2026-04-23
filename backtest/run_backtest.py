#!/usr/bin/env python3
"""
backtest/run_backtest.py
────────────────────────────────────────────────────────────────────
52-day (default) backtest of the four directional technical
strategies surfaced in the Telegram alerts:

  1. EMA Ribbon trend (EMA20 crosses above EMA50 with both > EMA200)
  2. RSI(14) mean-reversion (crosses up through 30)
  3. Donchian 20-period breakout (close > prior 20-period high)
  4. MACD(12/26/9) bullish cross

Runs on every ticker in config/tickers.py using Hyperliquid
candleSnapshot (15m bars). Chunked fetch handles >5k-bar limits.

Outputs a CSV summary (per ticker+strategy) plus an aggregate table.

Usage:
  python3 backtest/run_backtest.py                        # all tickers, 52d
  python3 backtest/run_backtest.py --days 30              # shorter window
  python3 backtest/run_backtest.py --tickers NVDA TSLA    # subset
  python3 backtest/run_backtest.py --out results.csv
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from config.tickers import TICKERS  # noqa: E402

HL_URL = "https://api.hyperliquid.xyz/info"
INTERVAL = "15m"
BAR_MS = 15 * 60 * 1000
CHUNK_BARS = 4500  # HL caps roughly at 5000


# ─── Fetch ────────────────────────────────────────────────────────

def _post(payload: dict, timeout: int = 30) -> list:
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", HL_URL,
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=timeout,
    )
    try:
        return json.loads(r.stdout)
    except Exception:
        return []


def fetch_candles_chunked(coin: str, days: int) -> list[tuple]:
    """Fetch 15m candles across `days` days, chunked to stay under HL's cap."""
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * 3600 * 1000
    out: list[tuple] = []
    cursor = start_ms
    chunk_span_ms = CHUNK_BARS * BAR_MS
    while cursor < now_ms:
        end = min(cursor + chunk_span_ms, now_ms)
        payload = {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": INTERVAL,
            "startTime": cursor, "endTime": end,
        }}
        arr = _post(payload)
        if not isinstance(arr, list) or not arr:
            break
        out.extend(
            (int(c["t"]), float(c["o"]), float(c["h"]), float(c["l"]),
             float(c["c"]), float(c["v"]))
            for c in arr
        )
        cursor = int(arr[-1]["T"]) + 1
    # De-dup by timestamp (chunk boundaries can overlap)
    seen = set(); dedup = []
    for row in out:
        if row[0] in seen:
            continue
        seen.add(row[0]); dedup.append(row)
    dedup.sort(key=lambda r: r[0])
    return dedup


# ─── Indicators (pure python, no deps) ────────────────────────────

def ema_series(vals: list[float], n: int) -> list[float | None]:
    if len(vals) < n:
        return [None] * len(vals)
    k = 2 / (n + 1)
    out: list[float | None] = [None] * (n - 1)
    e = sum(vals[:n]) / n
    out.append(e)
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def rsi_series(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) < n + 1:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0: g += d
        else: l -= d
    ag, al = g / n, l / n
    out[n] = 100 if al == 0 else 100 - (100 / (1 + ag / al))
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100 if al == 0 else 100 - (100 / (1 + ag / al))
    return out


def atr_series(cs: list[tuple], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(cs)
    if len(cs) < n + 1:
        return out
    trs = [0.0]
    for i in range(1, len(cs)):
        h, l, pc = cs[i][2], cs[i][3], cs[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    a = sum(trs[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(cs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out


# ─── Backtest engine ──────────────────────────────────────────────

def simulate(cs: list[tuple], sigs: list[int], atr_list: list[float | None],
             tp_mult: float, sl_mult: float, max_bars: int) -> dict:
    wins = losses = trades = 0
    pnl_sum = 0.0
    occupied_until = -1
    for sig_i in sigs:
        if sig_i <= occupied_until or sig_i + 1 >= len(cs):
            continue
        entry_i = sig_i + 1
        entry = cs[entry_i][1]
        a = atr_list[sig_i] if atr_list[sig_i] else entry * 0.01
        tp = entry + tp_mult * a
        sl = entry - sl_mult * a
        exited = False
        for j in range(entry_i, min(entry_i + max_bars, len(cs))):
            hi, lo = cs[j][2], cs[j][3]
            if lo <= sl:
                pnl = (sl - entry) / entry; losses += 1; exited = True; break
            if hi >= tp:
                pnl = (tp - entry) / entry; wins += 1; exited = True; break
        if not exited:
            exit_p = cs[min(entry_i + max_bars - 1, len(cs) - 1)][4]
            pnl = (exit_p - entry) / entry
            if pnl > 0: wins += 1
            else: losses += 1
        pnl_sum += pnl
        trades += 1
        occupied_until = entry_i + max_bars
    return {"trades": trades, "wins": wins, "losses": losses, "pnl_pct": pnl_sum * 100}


def strategy_signals(cs, closes, e20, e50, e200, rs, macd_line, sig_line):
    out: dict[str, list[int]] = {
        "EMA Ribbon": [], "RSI Reversion": [], "Donchian Breakout": [], "MACD Cross": [],
    }
    for i in range(1, len(cs)):
        if all(x[i - 1] is not None and x[i] is not None for x in (e20, e50, e200)):
            if (e20[i - 1] <= e50[i - 1] and e20[i] > e50[i]
                    and e20[i] > e200[i] and e50[i] > e200[i]):
                out["EMA Ribbon"].append(i)
        if rs[i - 1] is not None and rs[i] is not None and rs[i - 1] < 30 <= rs[i]:
            out["RSI Reversion"].append(i)
        if macd_line[i - 1] is not None and sig_line[i - 1] is not None \
                and macd_line[i] is not None and sig_line[i] is not None \
                and macd_line[i - 1] <= sig_line[i - 1] and macd_line[i] > sig_line[i]:
            out["MACD Cross"].append(i)
    for i in range(21, len(cs)):
        prev_hi = max(cs[k][2] for k in range(i - 20, i))
        if cs[i][4] > prev_hi:
            out["Donchian Breakout"].append(i)
    return out


STRATEGY_PARAMS = {
    "EMA Ribbon":        {"tp": 2.0, "sl": 1.5, "max_bars": 24},
    "RSI Reversion":     {"tp": 1.5, "sl": 1.0, "max_bars": 16},
    "Donchian Breakout": {"tp": 2.5, "sl": 1.5, "max_bars": 32},
    "MACD Cross":        {"tp": 2.0, "sl": 1.5, "max_bars": 24},
}


# ─── Driver ───────────────────────────────────────────────────────

def run(days: int, tickers: list[str] | None, out_csv: str) -> None:
    target = tickers or list(TICKERS.keys())
    rows = []
    agg: dict[str, dict] = {s: {"t": 0, "w": 0, "pnl": 0.0, "n": 0} for s in STRATEGY_PARAMS}

    for sym in target:
        cfg = TICKERS.get(sym)
        coin = (cfg["hl_asset"] if cfg else f"xyz:{sym}")
        print(f"[{sym}] fetching {days}d of {INTERVAL}...", flush=True)
        cs = fetch_candles_chunked(coin, days)
        if len(cs) < 300:
            print(f"[{sym}] skip — only {len(cs)} bars")
            continue
        closes = [x[4] for x in cs]
        e20 = ema_series(closes, 20)
        e50 = ema_series(closes, 50)
        e200 = ema_series(closes, 200)
        rs = rsi_series(closes, 14)
        a14 = atr_series(cs, 14)
        e12 = ema_series(closes, 12); e26 = ema_series(closes, 26)
        macd_line = [(a - b) if (a is not None and b is not None) else None
                     for a, b in zip(e12, e26)]
        sig_line: list[float | None] = [None] * len(macd_line)
        vi = [(i, v) for i, v in enumerate(macd_line) if v is not None]
        if len(vi) >= 9:
            sv = ema_series([v for _, v in vi], 9)
            for (i, _), s in zip(vi, sv):
                sig_line[i] = s

        sigs = strategy_signals(cs, closes, e20, e50, e200, rs, macd_line, sig_line)
        for name, params in STRATEGY_PARAMS.items():
            r = simulate(cs, sigs[name], a14, params["tp"], params["sl"], params["max_bars"])
            if r["trades"] == 0:
                wr = 0.0; avg = 0.0
            else:
                wr = r["wins"] / r["trades"] * 100
                avg = r["pnl_pct"] / r["trades"]
            rows.append({
                "ticker": sym, "strategy": name,
                "trades": r["trades"], "wins": r["wins"], "losses": r["losses"],
                "win_rate_pct": round(wr, 2), "avg_pnl_pct": round(avg, 3),
                "total_pnl_pct": round(r["pnl_pct"], 2),
            })
            agg[name]["t"] += r["trades"]; agg[name]["w"] += r["wins"]
            agg[name]["pnl"] += r["pnl_pct"]; agg[name]["n"] += 1
        print(f"[{sym}] done ({len(cs)} bars)")

    # Write per-ticker CSV
    out_path = Path(out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [
            "ticker", "strategy", "trades", "wins", "losses",
            "win_rate_pct", "avg_pnl_pct", "total_pnl_pct",
        ])
        w.writeheader()
        w.writerows(rows)
    print(f"\nPer-ticker rows written to {out_path}")

    # Aggregate table to stdout
    print(f"\n=== Aggregate ({days}d, {INTERVAL}, {len(target)} tickers) ===")
    print(f"{'Strategy':24s} {'trades':>7s} {'win%':>7s} {'avg_pnl%':>10s} {'tickers':>8s}")
    for name, a in agg.items():
        if a["t"] == 0:
            continue
        wr = a["w"] / a["t"] * 100
        avg = a["pnl"] / a["t"]
        print(f"{name:24s} {a['t']:7d} {wr:6.2f} {avg:+10.3f} {a['n']:8d}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=52)
    p.add_argument("--tickers", nargs="*", default=None)
    p.add_argument("--out", default="backtest/results.csv")
    args = p.parse_args()
    run(args.days, args.tickers, args.out)


if __name__ == "__main__":
    main()
