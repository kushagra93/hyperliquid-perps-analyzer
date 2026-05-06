#!/usr/bin/env python3
"""
tools/publish_dashboard.py
────────────────────────────────────────────────────────────────────
Reads eval/realtime_pn.jsonl + eval/sentiment_pn.jsonl, resolves
each fire's outcome by walking forward HL candles, and publishes
two JSON files for the Vercel-hosted static dashboard:

  dashboard/pn_feed.json       — ordered list of all PNs (most recent first)
  dashboard/performance.json   — aggregate + per-ticker + per-type win rates

Re-run periodically (cron, every 15 min):
  PYTHONPATH=. python3 tools/publish_dashboard.py

The dashboard reads pn_feed.json and performance.json on every page
load, so updating them is the entire ‘deploy data' step. Push to git
and Vercel auto-deploys.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.system_v2 import TICKER_CLUSTER, cluster_label, ticker_display

IST = timezone(timedelta(hours=5, minutes=30))
HL = "https://api.hyperliquid.xyz/info"

JSONL_PATHS = [
    ROOT / "eval" / "realtime_pn.jsonl",
    ROOT / "eval" / "sentiment_pn.jsonl",
]

PN_FEED_OUT = ROOT / "dashboard" / "pn_feed.json"
PERF_OUT    = ROOT / "dashboard" / "performance.json"


def _enrich_news_links(rec: dict) -> dict:
    """Attach Yahoo/Google news links to a PN record so the dashboard
    can render the 📰 source as a clickable link."""
    sym = rec.get("payload", {}).get("symbol") or rec.get("symbol")
    if not sym:
        return rec
    try:
        from events.news_search import (
            fetch_yahoo_news, fetch_top_headlines,
            TICKER_QUERY_NAME, QUERIES,
            _is_clickbait, _PROMO_RE,
        )
        cluster = rec.get("cluster") or "other"
        name = TICKER_QUERY_NAME.get(sym, sym)
        query = QUERIES.get(cluster, "{full} stock today").format(full=name)
        google_h = fetch_top_headlines(query, n=3)
        yahoo_h = fetch_yahoo_news(sym, n=3) if cluster not in ("commodity","fx","uranium","index") else []
        all_h = google_h + yahoo_h
        clean = [h for h in all_h
                  if not _is_clickbait(h.title or "")
                  and not _PROMO_RE.search(h.title or "")
                  and not _PROMO_RE.search(h.source or "")]
        pool = clean if clean else all_h
        items = []
        for h in pool[:5]:
            items.append({
                "title": h.title, "source": h.source, "age": h.when,
                "link": h.__dict__.get("_link", ""),
            })
        rec["news_items"] = items
    except Exception as e:
        rec["news_items"] = []
    return rec


def _hl_candles_after(coin: str, since_ms: int, n_bars: int = 16) -> list[dict]:
    end_ms = since_ms + (n_bars + 4) * 15 * 60 * 1000
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "15m",
        "startTime": since_ms, "endTime": end_ms,
    }}
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", HL,
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=12,
        )
        arr = json.loads(r.stdout)
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def _resolve_outcome(rec: dict) -> dict:
    """
    Walk forward HL candles after the PN's timestamp; resolve TP/SL/timeout
    using ATR-style brackets (1×ATR TP, 1.5×ATR SL, 16-bar timeout).
    """
    sym = rec.get("payload", {}).get("symbol") \
        or rec.get("symbol") \
        or rec.get("payload", {}).get("ticker")
    if not sym:
        return {**rec, "resolved": False, "reason": "no_symbol"}

    coin = f"xyz:{sym}"
    ts_iso = rec.get("ts_utc") or rec.get("ts_ist")
    if not ts_iso:
        return {**rec, "resolved": False}
    try:
        ts_ms = int(datetime.fromisoformat(ts_iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return {**rec, "resolved": False}

    cs = _hl_candles_after(coin, ts_ms, 16)
    if not cs:
        return {**rec, "resolved": False, "reason": "no_candles"}

    forward = [c for c in cs if int(c["t"]) > ts_ms][:16]
    if not forward:
        return {**rec, "resolved": False, "reason": "no_forward"}

    # Use the first close after the PN as entry
    entry = float(forward[0]["o"])

    # Direction proxy: sign of move_pct in payload, else from emoji in title
    payload = rec.get("payload") or {}
    direction = None
    if "move_pct" in payload:
        direction = "up" if (payload["move_pct"] or 0) > 0 else "down"
    elif "breakout_dir" in payload:
        direction = payload["breakout_dir"]
    elif rec.get("title", "").startswith("🟢"):
        direction = "up"
    elif rec.get("title", "").startswith("🔴"):
        direction = "down"
    if direction is None:
        return {**rec, "resolved": False, "reason": "no_direction"}

    # ATR estimate from the candles we have
    if len(forward) >= 5:
        trs = []
        for i in range(1, min(len(forward), 14)):
            h = float(forward[i]["h"]); l = float(forward[i]["l"])
            pc = float(forward[i-1]["c"])
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        atr = sum(trs) / len(trs) if trs else entry * 0.01
    else:
        atr = entry * 0.01

    if direction == "up":
        tp = entry + 1.0 * atr
        sl = entry - 1.5 * atr
    else:
        tp = entry - 1.0 * atr
        sl = entry + 1.5 * atr

    outcome = "timeout"; pnl_pct = 0.0; bars = 16
    for j, c in enumerate(forward, 1):
        hi = float(c["h"]); lo = float(c["l"])
        if direction == "up":
            if lo <= sl:
                outcome = "sl"; pnl_pct = (sl - entry) / entry * 100
                bars = j; break
            if hi >= tp:
                outcome = "tp1"; pnl_pct = (tp - entry) / entry * 100
                bars = j; break
        else:
            if hi >= sl:
                outcome = "sl"; pnl_pct = (entry - sl) / entry * 100
                bars = j; break
            if lo <= tp:
                outcome = "tp1"; pnl_pct = (entry - tp) / entry * 100
                bars = j; break
    if outcome == "timeout":
        last = forward[-1]
        exit_p = float(last["c"])
        pnl_pct = ((exit_p - entry) / entry * 100) if direction == "up" \
                  else ((entry - exit_p) / entry * 100)

    return {
        **rec,
        "resolved": True,
        "symbol": sym,
        "direction": direction,
        "entry": round(entry, 4),
        "atr": round(atr, 4),
        "outcome": outcome,
        "pnl_pct": round(pnl_pct, 3),
        "bars_to_resolution": bars,
    }


def load_jsonl_records() -> list[dict]:
    rows: list[dict] = []
    for p in JSONL_PATHS:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            # Skip tier_confirm follow-ups
            if rec.get("kind") == "tier_confirm":
                continue
            rows.append(rec)
    rows.sort(key=lambda r: r.get("ts_utc", ""), reverse=True)
    return rows


def aggregate_perf(resolved: list[dict]) -> dict:
    closed = [r for r in resolved if r.get("resolved") and r.get("outcome") in ("tp1", "sl", "timeout")]
    wins = [r for r in closed if (r.get("pnl_pct") or 0) > 0]
    by_kind = defaultdict(lambda: {"n": 0, "wins": 0, "pnl_sum": 0.0})
    by_ticker = defaultdict(lambda: {"n": 0, "wins": 0, "pnl_sum": 0.0})

    for r in closed:
        kind = r.get("kind", "?")
        sym = r.get("symbol", "?")
        by_kind[kind]["n"] += 1
        by_ticker[sym]["n"] += 1
        if (r.get("pnl_pct") or 0) > 0:
            by_kind[kind]["wins"] += 1
            by_ticker[sym]["wins"] += 1
        by_kind[kind]["pnl_sum"] += (r.get("pnl_pct") or 0)
        by_ticker[sym]["pnl_sum"] += (r.get("pnl_pct") or 0)

    def _stats(d):
        out = {}
        for k, v in d.items():
            if v["n"] == 0: continue
            out[k] = {
                "n": v["n"], "wins": v["wins"],
                "win_rate_pct": round(v["wins"] / v["n"] * 100, 1),
                "avg_pnl_pct": round(v["pnl_sum"] / v["n"], 3),
                "total_pnl_pct": round(v["pnl_sum"], 2),
            }
        return out

    overall_avg = round(sum((r.get("pnl_pct") or 0) for r in closed) / max(len(closed), 1), 3)
    overall_total = round(sum((r.get("pnl_pct") or 0) for r in closed), 2)
    return {
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "TP1=+1×ATR · SL=-1.5×ATR · 16-bar (4h) timeout",
        "overall": {
            "n": len(closed), "wins": len(wins),
            "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "avg_pnl_pct": overall_avg,
            "total_pnl_pct": overall_total,
        },
        "by_kind": _stats(by_kind),
        "by_ticker": _stats(by_ticker),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-feed", type=int, default=200,
                   help="max PNs in pn_feed.json (most recent first)")
    p.add_argument("--max-resolve", type=int, default=80,
                   help="max recent PNs to walk-forward-resolve")
    args = p.parse_args()

    rows = load_jsonl_records()
    print(f"Loaded {len(rows)} PNs from JSONL")

    # Resolve only the most-recent N (HL fetch budget)
    resolve_targets = rows[: args.max_resolve]
    resolved = []
    for r in resolve_targets:
        rs = _resolve_outcome(r)
        resolved.append(rs)

    # Older PNs included as-is, marked unresolved
    older = [{**r, "resolved": False, "reason": "older_than_resolve_window"}
              for r in rows[args.max_resolve : args.max_feed]]
    feed = resolved + older

    # Map symbols to display labels for the UI + add trade/chart links
    from notifiers.trade_links import tv_chart_link, hl_trade_link
    for r in feed:
        sym = r.get("symbol") or (r.get("payload") or {}).get("symbol")
        if sym:
            r["symbol"] = sym
            r["display"] = ticker_display(sym)
            r["cluster"] = TICKER_CLUSTER.get(sym, "other")
            r["group"] = cluster_label(r["cluster"])
            r["chart_url"] = tv_chart_link(sym)
            r["trade_url"] = hl_trade_link(sym)
            r = _enrich_news_links(r)

    PN_FEED_OUT.parent.mkdir(parents=True, exist_ok=True)
    PN_FEED_OUT.write_text(json.dumps({
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_total": len(rows),
        "n_resolved": sum(1 for r in resolved if r.get("resolved")),
        "items": feed,
    }, default=str, indent=2))
    print(f"Wrote {PN_FEED_OUT.relative_to(ROOT)} ({len(feed)} items)")

    perf = aggregate_perf(resolved)
    PERF_OUT.write_text(json.dumps(perf, indent=2))
    print(f"Wrote {PERF_OUT.relative_to(ROOT)} — overall WR {perf['overall']['win_rate_pct']}% "
          f"on {perf['overall']['n']} closed PNs")


if __name__ == "__main__":
    main()
