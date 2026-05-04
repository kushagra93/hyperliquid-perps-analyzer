#!/usr/bin/env python3
"""
analysis/offhours_report.py
────────────────────────────────────────────────────────────────────
Surface alerts that fired during pre-market or after-hours US
trading windows — the times the live time-gate (19-22 IST)
suppresses from Telegram.

Reads eval/alerts.jsonl (the source of truth — every fire logged
regardless of whether it was pushed) and resolves each alert's
outcome by looking up HL forward candles.

Bands (IST, where US 19:00-01:30 IST = regular session):
  • Pre-market   13:30-19:00 IST  (US 04:00-09:30 ET)
  • After-hours  01:30-05:30 IST  (US 16:00-20:00 ET)
  • Overnight    05:30-13:30 IST  (US 20:00-04:00 ET)

For each band: count, win rate, avg PnL, top movers. Push a
single Telegram message summarizing what would have been missed.

Usage:
  python3 analysis/offhours_report.py                    # last 24h
  python3 analysis/offhours_report.py --hours 72         # last 3d
  python3 analysis/offhours_report.py --hours 24 --telegram
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


# ── Helpers ──────────────────────────────────────────────────────

def _ist(ts_iso: str) -> datetime:
    return datetime.fromisoformat(ts_iso).astimezone(IST)


def _band(dt: datetime) -> str:
    h = dt.hour + dt.minute / 60
    if 13.5 <= h < 19.0:
        return "pre_market"
    if 19.0 <= h or h < 1.5:
        return "regular"
    if 1.5 <= h < 5.5:
        return "after_hours"
    return "overnight"


def _hl_candles_after(coin: str, since_ts_ms: int, n_bars: int = 24) -> list[dict]:
    """Fetch up to n_bars 15m candles after the given timestamp."""
    end = since_ts_ms + (n_bars + 4) * 15 * 60 * 1000
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "15m",
        "startTime": since_ts_ms, "endTime": end,
    }}
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=15,
        )
        return json.loads(r.stdout) or []
    except Exception:
        return []


def _resolve(alert: dict) -> dict:
    """Walk forward HL candles to determine outcome (TP1 / SL / timeout)."""
    pt = alert.get("price_trigger") or {}
    cond = alert.get("condition") or {}
    sym = alert.get("symbol", "?")
    coin = alert.get("hl_asset", f"xyz:{sym}")
    entry = float(pt.get("current_price") or 0)
    tech = alert.get("technical_outlook") or {}
    atr = float(tech.get("atr") or 0) or entry * 0.01
    direction = "up" if (pt.get("price_change_pct") or 0) >= 0 else "down"
    cid = cond.get("condition_id", "")

    # ts of fire
    ts_iso = alert.get("fired_at_iso") or alert.get("price_trigger", {}).get("triggered_at")
    if not ts_iso:
        return {**alert, "outcome": "no_ts"}
    try:
        ts_ms = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return {**alert, "outcome": "bad_ts"}

    cs = _hl_candles_after(coin, ts_ms, 24)
    cs = [c for c in cs if int(c["t"]) > ts_ms][:24]
    if not cs:
        return {**alert, "outcome": "open", "pnl_pct": None}

    if direction == "up":
        sl = entry - 1.5 * atr; tp = entry + 2.0 * atr
    else:
        sl = entry + 1.5 * atr; tp = entry - 2.0 * atr

    for j, c in enumerate(cs, 1):
        hi, lo = float(c["h"]), float(c["l"])
        if direction == "up":
            if lo <= sl:
                return {**alert, "outcome": "sl",
                         "pnl_pct": round((sl - entry) / entry * 100, 2),
                         "bars": j}
            if hi >= tp:
                return {**alert, "outcome": "tp1",
                         "pnl_pct": round((tp - entry) / entry * 100, 2),
                         "bars": j}
        else:
            if hi >= sl:
                return {**alert, "outcome": "sl",
                         "pnl_pct": round((entry - sl) / entry * 100, 2),
                         "bars": j}
            if lo <= tp:
                return {**alert, "outcome": "tp1",
                         "pnl_pct": round((entry - tp) / entry * 100, 2),
                         "bars": j}

    last = cs[-1]
    exit_p = float(last["c"])
    pnl = (exit_p - entry) / entry * 100 if direction == "up" else (entry - exit_p) / entry * 100
    return {**alert, "outcome": "timeout",
             "pnl_pct": round(pnl, 2), "bars": len(cs)}


def _band_stats(rows: list[dict]) -> dict:
    closed = [r for r in rows if r.get("outcome") in ("tp1", "sl", "timeout")]
    if not closed:
        return {"n": len(rows), "closed": 0, "win_rate_pct": 0.0,
                "total_pnl_pct": 0.0, "avg_pnl_pct": 0.0}
    wins = sum(1 for r in closed if (r.get("pnl_pct") or 0) > 0)
    total = sum((r.get("pnl_pct") or 0) for r in closed)
    return {
        "n": len(rows), "closed": len(closed), "wins": wins,
        "win_rate_pct": round(wins / len(closed) * 100, 1),
        "total_pnl_pct": round(total, 2),
        "avg_pnl_pct": round(total / len(closed), 2),
    }


# ── Driver ───────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=24,
                   help="lookback window in hours")
    p.add_argument("--jsonl", default="eval/alerts.jsonl")
    p.add_argument("--telegram", action="store_true")
    args = p.parse_args()

    path = Path(args.jsonl)
    if not path.exists():
        print(f"No JSONL log at {path}. Run the watcher first.")
        return

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)
    raw = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        ts_iso = o.get("fired_at_iso") or (o.get("price_trigger") or {}).get("triggered_at")
        if not ts_iso:
            continue
        try:
            dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < cutoff:
            continue
        o["_ist"] = dt.astimezone(IST)
        o["_band"] = _band(o["_ist"])
        raw.append(o)

    if not raw:
        print(f"No alerts in last {args.hours}h.")
        return

    print(f"Resolving {len(raw)} alerts from last {args.hours}h…")
    resolved = [_resolve(r) for r in raw]

    by_band = defaultdict(list)
    for r in resolved:
        by_band[r["_band"]].append(r)

    print(f"\n═══ OFF-HOURS REPORT ({args.hours}h) ═══")
    bands = ["pre_market", "regular", "after_hours", "overnight"]
    for b in bands:
        if not by_band[b]:
            continue
        s = _band_stats(by_band[b])
        print(f"\n{b.upper()}  n={s['n']}  closed={s['closed']}  "
              f"wr={s['win_rate_pct']}%  total PnL={s['total_pnl_pct']:+}%")

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

        msgs = []
        msgs.append(
            f"<b>🌒 OFF-HOURS REPORT — last {args.hours}h</b>\n"
            f"<i>Alerts that fired outside the proven 19-22 IST window. "
            f"These were logged but not pushed (TELEGRAM_TIME_GATE=true). "
            f"Outcomes resolved by walking forward HL candles.</i>"
        )

        band_emoji = {"pre_market": "🌅", "regular": "🇺🇸",
                      "after_hours": "🌙", "overnight": "💤"}
        band_label = {"pre_market": "PRE-MARKET (13:30-19:00 IST)",
                      "regular": "REGULAR (19:00-01:30 IST)",
                      "after_hours": "AFTER-HOURS (01:30-05:30 IST)",
                      "overnight": "OVERNIGHT (05:30-13:30 IST)"}

        for b in bands:
            if not by_band[b]:
                continue
            s = _band_stats(by_band[b])
            wr_color = "🟢" if s["win_rate_pct"] >= 50 else "🔴" if s["win_rate_pct"] > 0 else "⚪"
            block = [
                f"\n{band_emoji[b]} <b>{band_label[b]}</b>",
                f"  • Fires: {s['n']}  ·  Resolved: {s['closed']}",
                f"  • Win rate: {wr_color} <b>{s['win_rate_pct']}%</b>",
                f"  • Total PnL: <b>{s['total_pnl_pct']:+}%</b>  "
                f"·  Avg: {s['avg_pnl_pct']:+}%",
            ]
            # Top 3 movers in this band
            movers = sorted(by_band[b],
                            key=lambda r: -abs((r.get("price_trigger") or {}).get("price_change_pct") or 0))[:3]
            if movers:
                block.append("  <i>Top movers:</i>")
                for m in movers:
                    sym = m.get("symbol", "?")
                    pct = (m.get("price_trigger") or {}).get("price_change_pct") or 0
                    out = m.get("outcome", "open")
                    pnl = m.get("pnl_pct")
                    pnl_s = f"{pnl:+.2f}%" if pnl is not None else "n/a"
                    out_emoji = {"tp1": "🟢", "sl": "🔴", "timeout": "⚪", "open": "⏳"}.get(out, "?")
                    when = m["_ist"].strftime("%d %b %H:%M IST")
                    block.append(f"    {out_emoji} <b>{sym}</b> {pct:+.2f}% @ {when} → {out} ({pnl_s})")
            msgs.append("\n".join(block))

        # Verdict
        verdict_parts = ["<b>📋 What this tells us</b>"]
        for b in bands:
            if not by_band[b]:
                continue
            s = _band_stats(by_band[b])
            if s["closed"] == 0:
                continue
            if s["win_rate_pct"] >= 50 and s["total_pnl_pct"] > 0:
                verdict_parts.append(f"  • <b>{b}</b>: edge present ({s['win_rate_pct']}% wr, {s['total_pnl_pct']:+}%) — "
                                      f"consider extending the gate to include this window.")
            elif s["total_pnl_pct"] < 0:
                verdict_parts.append(f"  • <b>{b}</b>: confirms gate decision ({s['win_rate_pct']}% wr, {s['total_pnl_pct']:+}%) — "
                                      f"keep suppressed.")
            else:
                verdict_parts.append(f"  • <b>{b}</b>: inconclusive ({s['win_rate_pct']}% wr, {s['total_pnl_pct']:+}%) — "
                                      f"more data needed.")
        msgs.append("\n".join(verdict_parts))

        for m in msgs:
            tg(m); time.sleep(0.4)
        print(f"\nPushed {len(msgs)} messages to Telegram")


if __name__ == "__main__":
    main()
