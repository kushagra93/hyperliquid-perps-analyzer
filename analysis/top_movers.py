#!/usr/bin/env python3
"""
analysis/top_movers.py
────────────────────────────────────────────────────────────────────
Every 30 minutes, scan the universe, rank top movers, and push a
compact digest with sentiment + reason inference.

What's in each digest:
  • Headline narrative (rule-based, no LLM):
      • broad risk-on/off if N+ clusters move same direction
      • cluster rotation (e.g. "semis -, mega-tech flat") if asymmetric
      • single-name idio if one ticker dominates
  • Top 5-7 movers, each with:
      • signed move %
      • condition-derived sentiment tag (C1/C2/C3/C4)
      • OI direction icon
  • Macro context: any matching upcoming events from events/fetcher
    (when Finnhub key is available)

Reason inference is deterministic and cited (no LLM hallucination
risk). When a Finnhub key is present, ticker-specific news headlines
are pulled via /company-news and a single keyword-sentiment line is
appended.

Run modes:
  python3 analysis/top_movers.py                     # one-shot
  python3 analysis/top_movers.py --daemon            # every 30m
  python3 analysis/top_movers.py --window-min 30 --top 7 --telegram

Channel routing: TELEGRAM_PN_CHANNEL_ID falls back to TELEGRAM_CHAT_ID.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import logging
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
from analysis.system_v2 import TICKER_CLUSTER

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


# ── HL fetch ─────────────────────────────────────────────────────

def _hl_post(payload: dict, timeout: int = 15) -> dict | list | None:
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=timeout,
        )
        return json.loads(r.stdout)
    except Exception:
        return None


def _candles(coin: str, since_ms: int, until_ms: int) -> list[dict]:
    arr = _hl_post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "15m",
        "startTime": since_ms, "endTime": until_ms,
    }})
    return arr if isinstance(arr, list) else []


def _meta_and_ctxs() -> tuple[dict, list]:
    data = _hl_post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    if not isinstance(data, list) or len(data) < 2:
        return {}, []
    return data[0], data[1] or []


# ── Compute mover row per ticker ────────────────────────────────

def compute_movers(window_min: int = 30,
                    universe_mode: str = "all",
                    min_volume_24h: float = 100_000) -> list[dict]:
    """
    Scan tickers and compute the per-ticker move over the last
    `window_min` minutes.

    universe_mode:
      "all"        — every non-delisted xyz: ticker on Hyperliquid
                      (70+ names). Tickers below `min_volume_24h`
                      are filtered to avoid illiquid noise.
      "configured" — only the 15 in config/tickers.py.
    """
    now_ms = int(time.time() * 1000)
    since_ms = now_ms - (window_min + 30) * 60 * 1000

    meta, ctxs = _meta_and_ctxs()
    universe = (meta or {}).get("universe", [])

    # Build candidate ticker list
    if universe_mode == "all":
        candidates: list[tuple[str, dict]] = []
        for i, a in enumerate(universe):
            if a.get("isDelisted"):
                continue
            name = a.get("name", "")
            ctx = ctxs[i] if i < len(ctxs) else {}
            vol = float(ctx.get("dayNtlVlm") or 0)
            if vol < min_volume_24h:
                continue
            sym = name.split(":")[-1]  # "xyz:NVDA" → "NVDA"
            candidates.append((sym, {"hl_asset": name, "ctx": ctx}))
    else:
        name_to_idx = {a.get("name"): i for i, a in enumerate(universe)}
        candidates = []
        for sym, cfg in TICKERS.items():
            coin = cfg["hl_asset"]
            idx = name_to_idx.get(coin)
            if idx is None:
                continue
            candidates.append((sym, {"hl_asset": coin, "ctx": ctxs[idx] if idx < len(ctxs) else {}}))

    rows: list[dict] = []
    for sym, info in candidates:
        coin = info["hl_asset"]
        ctx = info["ctx"]
        cur_price = float(ctx.get("markPx") or 0)
        funding = float(ctx.get("funding") or 0)
        oi = float(ctx.get("openInterest") or 0)
        vol24 = float(ctx.get("dayNtlVlm") or 0)

        cs = _candles(coin, since_ms, now_ms)
        if not cs or cur_price <= 0:
            continue
        target_ms = now_ms - window_min * 60 * 1000
        ref_c = min(cs, key=lambda c: abs(int(c["t"]) - target_ms))
        ref_price = float(ref_c["c"])
        if ref_price <= 0:
            continue
        move_pct = (cur_price - ref_price) / ref_price * 100

        rows.append({
            "symbol": sym,
            "cluster": TICKER_CLUSTER.get(sym, "other"),
            "price": cur_price,
            "move_pct": round(move_pct, 2),
            "oi": oi,
            "funding": funding,
            "vol24": vol24,
        })
    return rows


def classify_condition(move_pct: float, funding: float) -> str:
    """
    Coarse sentiment proxy without true OI history. Funding sign is
    a weak proxy for OI direction (positive funding = longs paying =
    crowded long).
    """
    if move_pct > 0.3 and funding > 0:
        return "C1"  # bull conviction
    if move_pct < -0.3 and funding > 0:
        return "C2"  # bear (longs paying into a fall = real shorts)
    if move_pct < -0.3 and funding <= 0:
        return "C3"  # weak fall, longs exiting
    if move_pct > 0.3 and funding <= 0:
        return "C4"  # weak rally, shorts covering
    return "FLAT"


SENTIMENT_TAG = {
    "C1": ("🟢", "fresh longs"),
    "C2": ("🔴", "fresh shorts"),
    "C3": ("📉", "longs exiting"),
    "C4": ("📈", "shorts covering"),
    "FLAT": ("⚪", "drift"),
}


# ── Narrative inference ─────────────────────────────────────────

def cluster_heatmap(rows: list[dict]) -> dict[str, dict]:
    """Per-cluster aggregate: avg move, count up/down."""
    by_c: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_c[r["cluster"]].append(r)
    out = {}
    for cluster, items in by_c.items():
        moves = [r["move_pct"] for r in items]
        up = sum(1 for m in moves if m > 0.3)
        dn = sum(1 for m in moves if m < -0.3)
        out[cluster] = {
            "n": len(items),
            "avg_move": round(statistics.mean(moves), 2),
            "up": up, "dn": dn,
            "tilt": "up" if up > dn else "dn" if dn > up else "flat",
        }
    return out


def infer_narrative(rows: list[dict], heatmap: dict) -> str:
    """Top-line narrative from cluster heatmap."""
    if not rows:
        return "No data."

    universe_avg = statistics.mean([r["move_pct"] for r in rows])

    # Cluster breadth: how many clusters tilt the same way?
    tilts = [v["tilt"] for v in heatmap.values()]
    n_up = sum(1 for t in tilts if t == "up")
    n_dn = sum(1 for t in tilts if t == "dn")

    if n_dn >= 3 and n_up <= 1 and universe_avg < -0.3:
        return f"🔴 Broad risk-off · {n_dn} clusters red"
    if n_up >= 3 and n_dn <= 1 and universe_avg > 0.3:
        return f"🟢 Broad risk-on · {n_up} clusters green"

    # Asymmetric rotation: name the strongest cluster move
    extremes = sorted(heatmap.items(), key=lambda kv: kv[1]["avg_move"])
    if extremes:
        worst = extremes[0]; best = extremes[-1]
        if abs(worst[1]["avg_move"]) > 0.5 or abs(best[1]["avg_move"]) > 0.5:
            return (f"⚖️ Rotation · {best[0]} {best[1]['avg_move']:+.1f}% / "
                     f"{worst[0]} {worst[1]['avg_move']:+.1f}%")

    # Single-name idio
    biggest = max(rows, key=lambda r: abs(r["move_pct"]))
    if abs(biggest["move_pct"]) > 1.5 and abs(biggest["move_pct"]) > 2 * abs(universe_avg):
        return f"🎯 Single-name idio · {biggest['symbol']} {biggest['move_pct']:+.1f}%"

    return "⚪ Mixed tape · no dominant theme"


# ── Optional: Finnhub headline appendix ─────────────────────────

def _fetch_headlines(symbol: str, days: int = 1) -> list[str]:
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return []
    today = datetime.now(timezone.utc).date()
    try:
        r = subprocess.run(
            ["curl", "-s", "-G", "https://finnhub.io/api/v1/company-news",
             "--data-urlencode", f"symbol={symbol}",
             "--data-urlencode", f"from={(today - timedelta(days=days)).isoformat()}",
             "--data-urlencode", f"to={today.isoformat()}",
             "--data-urlencode", f"token={key}"],
            capture_output=True, text=True, timeout=8,
        )
        arr = json.loads(r.stdout) or []
        return [a.get("headline", "") for a in arr[:3]]
    except Exception:
        return []


# ── Render ──────────────────────────────────────────────────────

def render_digest(rows: list[dict], top_n: int = 5,
                   include_headlines: bool = False) -> str:
    rows_sorted = sorted(rows, key=lambda r: -abs(r["move_pct"]))
    top = rows_sorted[:top_n]

    heatmap = cluster_heatmap(rows)
    narrative = infer_narrative(rows, heatmap)
    now_ist = datetime.now(IST).strftime("%H:%M IST")

    lines = [
        f"<b>📊 Top movers · {now_ist}</b>",
        f"<i>{narrative}</i>",
        "",
    ]
    for r in top:
        cond = classify_condition(r["move_pct"], r["funding"])
        emoji, tag = SENTIMENT_TAG[cond]
        sign = "+" if r["move_pct"] > 0 else ""
        lines.append(
            f"{emoji} <b>{r['symbol']:5}</b> {sign}{r['move_pct']:.1f}% "
            f"· {tag} ({cond})"
        )

    # Cluster breakdown (1-2 lines max)
    cluster_line = " · ".join(
        f"{c} {v['avg_move']:+.1f}%"
        for c, v in sorted(heatmap.items(), key=lambda kv: kv[1]["avg_move"])
        if v["n"] >= 2
    )
    if cluster_line:
        lines.append("")
        lines.append(f"<b>Clusters:</b> {cluster_line}")

    if include_headlines:
        # Pull headlines only for the single biggest mover (rate-limit polite)
        big = top[0] if top else None
        if big:
            hl = _fetch_headlines(big["symbol"])
            if hl:
                lines.append("")
                lines.append(f"<b>{big['symbol']} news (last 24h):</b>")
                for h in hl[:2]:
                    lines.append(f"  • {h[:90]}")

    return "\n".join(lines)


# ── Telegram send ───────────────────────────────────────────────

# ── Spotlight mode: single compact PN for biggest mover ──────

def render_spotlight(rows: list[dict]) -> str:
    """
    Single compact PN (≤130 chars) for the biggest mover. Used as
    the "occasional push" when no real signal fires — keeps the
    channel feeling alive without spamming.
    """
    if not rows:
        return ""
    from notifiers.compact_intel import (
        IntelInputs, fetch_intel_signals, build_intel_body
    )
    big = max(rows, key=lambda r: abs(r["move_pct"]))
    sym = big["symbol"]
    cluster = big["cluster"]
    cond = classify_condition(big["move_pct"], big["funding"])

    intel = fetch_intel_signals(sym, f"xyz:{sym}")
    inp = IntelInputs(
        symbol=sym, move_pct=big["move_pct"],
        condition_id=cond if cond != "FLAT" else "",
        funding=big["funding"],
        volume_zscore=intel.get("volume_zscore"),
        atr_ratio=intel.get("atr_ratio"),
        near_vwap=intel.get("near_vwap"),
        range_compression=intel.get("range_compression", False),
    )
    body = build_intel_body(inp)
    emoji = "🔴" if big["move_pct"] < -0.3 else ("🟢" if big["move_pct"] > 0.3 else "⚪")
    title = f"{emoji} Spotlight · {sym} {big['move_pct']:+.1f}% ({cluster})"
    if len(title) > 50: title = title[:49] + "…"
    return f"<b>{title}</b>\n{body}"


def _send(text: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_PN_CHANNEL_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    r = subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "--data-urlencode", f"chat_id={chat}",
        "--data-urlencode", f"text={text}",
        "--data-urlencode", "parse_mode=HTML",
        "--data-urlencode", "disable_web_page_preview=true",
    ], capture_output=True, text=True, timeout=15)
    return '"ok":true' in r.stdout


# ── Driver ──────────────────────────────────────────────────────

def run_once(window_min: int, top_n: int, headlines: bool, telegram: bool) -> None:
    rows = compute_movers(window_min)
    if not rows:
        print("No HL data — skipping.")
        return
    digest = render_digest(rows, top_n=top_n, include_headlines=headlines)
    print(digest)
    if telegram:
        ok = _send(digest)
        print(f"\nTelegram: {'sent' if ok else 'FAILED'}")


def run_daemon(window_min: int, top_n: int, headlines: bool, interval_min: int) -> None:
    print(f"Top-movers daemon · every {interval_min}m · window {window_min}m")
    while True:
        try:
            run_once(window_min, top_n, headlines, telegram=True)
        except Exception as e:
            logger.warning(f"[movers] tick failed: {e}")
        time.sleep(interval_min * 60)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--window-min", type=int, default=30)
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--universe", default="all", choices=("all", "configured"),
                   help="all = every non-delisted xyz: ticker (70+); "
                         "configured = only the 15 in config/tickers.py")
    p.add_argument("--min-volume", type=float, default=100_000,
                   help="filter illiquid names by 24h notional volume")
    p.add_argument("--headlines", action="store_true",
                   help="append Finnhub headlines for biggest mover (needs key)")
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--daemon", action="store_true")
    p.add_argument("--interval-min", type=int, default=30)
    p.add_argument("--spotlight", action="store_true",
                   help="render a single compact PN for the biggest mover (instead of the full digest)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

    def _once(send_tg: bool):
        rows = compute_movers(args.window_min, args.universe, args.min_volume)
        if not rows:
            print("No HL data — skipping.")
            return
        if args.spotlight:
            text = render_spotlight(rows)
        else:
            text = render_digest(rows, top_n=args.top,
                                  include_headlines=args.headlines)
        print(text)
        if send_tg and text:
            ok = _send(text)
            print(f"\nTelegram: {'sent' if ok else 'FAILED'}")

    if args.daemon:
        print(f"Top-movers daemon · every {args.interval_min}m · window {args.window_min}m · universe={args.universe}")
        while True:
            try:
                _once(send_tg=True)
            except Exception as e:
                logger.warning(f"[movers] tick failed: {e}")
            time.sleep(args.interval_min * 60)
    else:
        _once(args.telegram)


if __name__ == "__main__":
    main()
