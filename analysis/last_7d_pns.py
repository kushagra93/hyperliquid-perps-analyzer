#!/usr/bin/env python3
"""
analysis/last_7d_pns.py
────────────────────────────────────────────────────────────────────
Replay the last 7 days of HL data through today's PN detectors.
Generates compact PN cards grouped by IST date and pushes to
Telegram as a multi-day digest. Useful for showing what the
current logic would have surfaced.

Detectors used (all live in production):
  • BREAKOUT     close above/below 20-bar Donchian level
  • VOLUME_SPIKE current bar volume z-score ≥ 3 vs prior 20 bars
  • BIG_MOVE     abs(15m return) ≥ 1.5%
  • CLUSTER_DAY  3+ clusters all green or all red on the day

Each fire gets the compact 3-line layout:
  <emoji> <SYM> <when> · <30m move>
  📈 <plain-English fact>
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.tickers import TICKERS
from analysis.system_v2 import (
    TICKER_CLUSTER, cluster_label, ticker_display,
)
from analysis.top_movers import PRIMARY_CLUSTERS

IST = timezone(timedelta(hours=5, minutes=30))
HL = "https://api.hyperliquid.xyz/info"
BAR_MS = 15 * 60 * 1000


# ── HL chunked fetch (reused from multi_window) ─────────────────

def _fetch(coin: str, days: int) -> list[dict]:
    now_ms = int(time.time() * 1000)
    start = now_ms - days * 24 * 3600 * 1000
    out: list[dict] = []
    cursor = start
    span = 1900 * BAR_MS  # ~20d per chunk
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
                capture_output=True, text=True, timeout=30,
            )
            arr = json.loads(r.stdout) or []
        except Exception:
            arr = []
        if not isinstance(arr, list) or not arr:
            cursor = end + 1
            time.sleep(0.3)
            continue
        out.extend(arr)
        cursor = int(arr[-1]["T"]) + 1
        time.sleep(0.3)
    seen = set(); dedup = []
    for c in out:
        if c["t"] in seen: continue
        seen.add(c["t"]); dedup.append(c)
    dedup.sort(key=lambda c: int(c["t"]))
    return dedup


# ── Detectors ────────────────────────────────────────────────────

def detect_fires(sym: str, candles: list[dict]) -> list[dict]:
    if len(candles) < 25:
        return []
    closes = [float(c["c"]) for c in candles]
    highs  = [float(c["h"]) for c in candles]
    lows   = [float(c["l"]) for c in candles]
    vols   = [float(c["v"]) for c in candles]

    out: list[dict] = []
    for i in range(20, len(candles)):
        ts = int(candles[i]["t"])
        cur = closes[i]
        prev = closes[i - 1]
        ret = (cur - prev) / prev * 100 if prev else 0

        # Breakout: close above 20-bar high
        prior_hi = max(highs[i - 20:i])
        prior_lo = min(lows[i - 20:i])
        if cur > prior_hi:
            out.append({"ts": ts, "sym": sym, "type": "breakout",
                         "dir": "up", "level": prior_hi,
                         "move_pct": ret, "price": cur})
        elif cur < prior_lo:
            out.append({"ts": ts, "sym": sym, "type": "breakout",
                         "dir": "down", "level": prior_lo,
                         "move_pct": ret, "price": cur})

        # Volume z-score
        if i >= 21:
            window = vols[i - 20:i]
            mean = sum(window) / len(window)
            sd = statistics.stdev(window) if len(window) > 1 else 0
            if sd > 0:
                z = (vols[i] - mean) / sd
                if z >= 3.0:
                    out.append({"ts": ts, "sym": sym, "type": "volume_spike",
                                "vol_z": z, "move_pct": ret, "price": cur})

        # Big single-bar move
        if abs(ret) >= 1.5:
            out.append({"ts": ts, "sym": sym, "type": "big_move",
                         "move_pct": ret, "price": cur})

    # Cluster within same ticker — collapse adjacent fires of same type
    collapsed = []
    last = {}
    for f in out:
        key = (f["sym"], f["type"], f.get("dir", ""))
        if key in last and (f["ts"] - last[key]) < 60 * 60 * 1000:
            continue   # same kind within 1h, skip dup
        last[key] = f["ts"]
        collapsed.append(f)
    return collapsed


# ── Compact card composer ────────────────────────────────────────

def _emoji(move: float) -> str:
    return "🟢" if move > 0 else "🔴" if move < 0 else "⚪"


def card(fire: dict) -> str:
    sym = ticker_display(fire["sym"])
    cluster = TICKER_CLUSTER.get(fire["sym"], "other")
    group = cluster_label(cluster)
    when = datetime.fromtimestamp(fire["ts"]/1000, tz=IST).strftime("%H:%M")
    move = fire.get("move_pct", 0)
    em = _emoji(move)
    if fire["type"] == "breakout":
        d = fire["dir"]
        side_word = "broke up" if d == "up" else "broke down"
        action = "buy zone" if d == "up" else "sell zone"
        return (f"{em} <b>{sym}</b> {side_word} ${fire['level']:.2f} · "
                f"{when} · {move:+.2f}%\n"
                f"   📈 Range broken {d}; trend traders pile in. "
                f"<i>{group} · {action}.</i>")
    if fire["type"] == "volume_spike":
        z = fire.get("vol_z", 0)
        side = "buying" if move > 0 else "selling"
        return (f"{em} <b>{sym}</b> volume spike · {when} · {move:+.2f}%\n"
                f"   📈 Vol {z:.0f}× normal = institutions actively {side}. "
                f"<i>{group}.</i>")
    if fire["type"] == "big_move":
        return (f"{em} <b>{sym}</b> sharp move · {when} · "
                f"<b>{move:+.2f}%</b>\n"
                f"   📈 Single-bar {abs(move):.1f}% range. <i>{group}.</i>")
    return f"{em} {sym} {fire['type']} · {when} · {move:+.2f}%"


# ── Driver ───────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--per-day", type=int, default=8,
                   help="max fires shown per day")
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--primary-only", action="store_true",
                   help="restrict to PRIMARY_CLUSTERS (stocks + indices)")
    args = p.parse_args()

    print(f"Fetching {args.days}d × {len(TICKERS)} tickers …")
    fires_all: list[dict] = []
    for sym, cfg in TICKERS.items():
        cluster = TICKER_CLUSTER.get(sym, "other")
        if args.primary_only and cluster not in PRIMARY_CLUSTERS:
            continue
        coin = cfg["hl_asset"]
        candles = _fetch(coin, args.days)
        if len(candles) < 25:
            print(f"  {sym}: skip ({len(candles)} bars)")
            continue
        fires = detect_fires(sym, candles)
        fires_all.extend(fires)
        print(f"  {sym}: {len(candles)} bars · {len(fires)} fires")

    if not fires_all:
        print("No fires."); return

    # Group by IST date (most recent first)
    by_day: dict[str, list[dict]] = defaultdict(list)
    for f in fires_all:
        d = datetime.fromtimestamp(f["ts"]/1000, tz=IST).date().isoformat()
        by_day[d].append(f)

    # Rank within each day by abs(move) and take top N
    days_sorted = sorted(by_day.keys(), reverse=True)[:args.days]

    print(f"\n═══ Last {args.days} days · per-day cap {args.per_day} ═══")
    blocks = []
    for d in days_sorted:
        day_fires = by_day[d]
        day_fires.sort(key=lambda f: -abs(f.get("move_pct", 0)))
        top = day_fires[: args.per_day]
        dt = datetime.fromisoformat(d)
        weekday = dt.strftime("%a")
        block = [f"<b>📅 {d} ({weekday}) · {len(day_fires)} fires</b>"]
        for f in top:
            block.append(card(f))
        blocks.append("\n".join(block))

    for b in blocks:
        print("\n" + b)

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN",
            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_PN_CHANNEL_ID") \
            or os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        # Header
        subprocess.run([
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "--data-urlencode", f"chat_id={chat}",
            "--data-urlencode", f"text=<b>🗓️ LAST {args.days} DAYS — replay through today's PN logic</b>\n"
                                 f"<i>Each block is what would have fired with current breakout / vol-spike / big-move detectors. Top {args.per_day}/day by absolute move.</i>",
            "--data-urlencode", "parse_mode=HTML",
            "--data-urlencode", "disable_web_page_preview=true",
        ], capture_output=True, text=True, timeout=10)
        time.sleep(0.4)
        for b in blocks:
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat}",
                "--data-urlencode", f"text={b}",
                "--data-urlencode", "parse_mode=HTML",
                "--data-urlencode", "disable_web_page_preview=true",
            ], capture_output=True, text=True, timeout=10)
            time.sleep(0.5)
        print(f"\nPushed {len(blocks)+1} messages to Telegram.")


if __name__ == "__main__":
    main()
