#!/usr/bin/env python3
"""
tools/live_watch.py
────────────────────────────────────────────────────────────────────
Lightweight live-signal watcher for demo/preview purposes.

Unlike main.py (which needs Google Sheets + OpenRouter + SerpAPI keys
for the full TickerWorker flow), this script runs with:
  - FINNHUB_API_KEY    (for events)
  - TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   (for delivery)
  - Hyperliquid (no key needed)

For each tick (every `POLL_SEC` seconds, default 60):
  1. Fetch 15m candles for each tracked ticker.
  2. Compute a signed move over the last `WINDOW_MIN` minutes
     (default 20) from the 15m close series.
  3. If |move| >= PRICE_THRESHOLD_PCT → construct an alert with:
       - rule-based `deterministic_verdict` lean (ported from Agent 3
         fallback; no LLM call)
       - real event_context (earnings + tags)
       - real technical_outlook (5 strategies)
     and send via notifiers.telegram.
  4. Per-ticker cooldown prevents spam.

Intended as a short-lived demo (TIMEOUT_SEC default 900). For
production alerting use main.py with the full key set.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.hl_client import fetch_candles, fetch_meta_and_asset_ctxs
from core.technicals import get_technical_outlook
from events.context import get_event_context
from notifiers.telegram import send_alert_if_enabled, send_message
from config.tickers import TICKERS


POLL_SEC = int(os.environ.get("LIVE_POLL_SEC", 60))
WINDOW_MIN = int(os.environ.get("LIVE_WINDOW_MIN", 20))
PRICE_THRESHOLD_PCT = float(os.environ.get("LIVE_PRICE_THRESHOLD_PCT", 1.5))
COOLDOWN_SEC = int(os.environ.get("LIVE_COOLDOWN_SEC", 600))


def _deterministic_verdict(price_pct: float, oi_pct: float, funding: float,
                            has_news: bool) -> dict:
    """Ported subset of agents.agent3_causality.deterministic_verdict
    so this script doesn't pull the LLM-bearing module at all."""
    if price_pct > 0 and oi_pct > 0:
        cid, label = "C1", "Strong bull"
        verdict = f"Price +{price_pct:.2f}% with OI +{oi_pct:.2f}% — new longs entering."
        driver, conf = "oi_flow", "high"
    elif price_pct < 0 and oi_pct > 0:
        cid, label = "C2", "Strong bear"
        verdict = f"Price {price_pct:.2f}% with OI +{oi_pct:.2f}% — new shorts entering."
        driver, conf = "oi_flow", "high"
    elif price_pct < 0 and oi_pct < 0:
        cid, label = "C3", "Weak fall"
        verdict = f"Price {price_pct:.2f}% with OI {oi_pct:.2f}% — longs exiting, weak move."
        driver, conf = "oi_flow", "low"
    else:
        cid, label = "C4", "Weak rally"
        verdict = f"Price +{price_pct:.2f}% with OI {oi_pct:.2f}% — shorts covering, weak rally."
        driver, conf = "oi_flow", "low"

    flags = []
    if cid == "C1" and funding < 0: flags.append("funding_disagrees")
    if cid == "C2" and funding > 0: flags.append("funding_disagrees")
    if has_news: flags.append("news_present")
    flags.append("rule_based_live_watcher")
    return {
        "condition_id": cid, "label": label,
        "verdict": verdict, "confidence": conf, "primary_driver": driver,
        "flags": flags, "reasoning": f"Rule-based: {label}, funding {funding*100:+.4f}%.",
    }


def _oi_snapshot(ctx, prev_oi: float | None) -> dict:
    oi = float(ctx.get("openInterest") or 0)
    if prev_oi is None or prev_oi == 0:
        return {"current_oi": oi, "oi_change_pct": 0.0, "direction": "flat",
                "baseline_oi": oi, "funding_rate": float(ctx.get("funding") or 0),
                "volume_24h": float(ctx.get("dayNtlVlm") or 0),
                "premium": float(ctx.get("premium") or 0),
                "interpretation": "no baseline yet"}
    pct = (oi - prev_oi) / prev_oi * 100 if prev_oi else 0.0
    return {
        "current_oi": oi, "baseline_oi": prev_oi, "oi_change_pct": pct,
        "direction": "up" if pct > 0.5 else "down" if pct < -0.5 else "flat",
        "funding_rate": float(ctx.get("funding") or 0),
        "volume_24h": float(ctx.get("dayNtlVlm") or 0),
        "premium": float(ctx.get("premium") or 0),
        "interpretation": f"OI {pct:+.2f}% since last tick.",
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--timeout", type=int, default=900,
                   help="seconds before the watcher exits (default 900 = 15min)")
    p.add_argument("--threshold", type=float, default=PRICE_THRESHOLD_PCT,
                   help=f"price move % to fire (default {PRICE_THRESHOLD_PCT})")
    args = p.parse_args()

    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        # Use the token/chat that's been in use all session if not set
        os.environ.setdefault("TELEGRAM_BOT_TOKEN",
            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        os.environ.setdefault("TELEGRAM_CHAT_ID", "-1003819293218")

    last_alert_ts: dict[str, float] = {}
    prev_oi: dict[str, float] = {}
    start = time.time()
    tick_n = 0

    send_message(
        f"<b>🛰️ LIVE WATCHER ACTIVE</b> · threshold ±{args.threshold:.1f}% / {WINDOW_MIN}m · "
        f"{len(TICKERS)} tickers · {args.timeout}s session"
    )

    while time.time() - start < args.timeout:
        tick_n += 1
        shared = fetch_meta_and_asset_ctxs()
        if not shared:
            time.sleep(POLL_SEC); continue

        universe = shared[0].get("universe", [])
        ctxs = shared[1] or []
        hl_idx = {a.get("name"): i for i, a in enumerate(universe)}

        for sym, cfg in TICKERS.items():
            coin = cfg.get("hl_asset", f"xyz:{sym}")
            idx = hl_idx.get(coin)
            if idx is None or idx >= len(ctxs):
                continue
            ctx = ctxs[idx]
            price = float(ctx.get("markPx") or 0)
            if not price:
                continue

            # Compute move over WINDOW_MIN minutes via 15m candles
            c15 = fetch_candles(coin, "15m", (WINDOW_MIN + 60) * 60 * 1000)
            if len(c15) < 3:
                continue
            closes = [c[4] for c in c15]
            bars_back = max(1, WINDOW_MIN // 15)
            ref = closes[-1 - bars_back] if len(closes) > bars_back else closes[0]
            move_pct = (price - ref) / ref * 100 if ref else 0.0

            if abs(move_pct) < args.threshold:
                continue
            if time.time() - last_alert_ts.get(sym, 0) < COOLDOWN_SEC:
                continue

            oi_snap = _oi_snapshot(ctx, prev_oi.get(sym))
            oi_pct = oi_snap["oi_change_pct"]
            verdict_dict = _deterministic_verdict(
                move_pct, oi_pct,
                oi_snap["funding_rate"], has_news=False,
            )

            alert = {
                "symbol": sym, "full_name": cfg.get("full_name", sym),
                "hl_asset": coin,
                "price_trigger": {
                    "current_price": price,
                    "price_change_pct": move_pct,
                    "trigger_source": "price",
                    "window_start_price": ref,
                },
                "oi_report": oi_snap,
                "condition": {
                    "condition_id": verdict_dict["condition_id"],
                    "label": verdict_dict["label"],
                    "description": "live watcher",
                    "oi_change_pct": oi_pct,
                },
                "causality": {
                    "verdict": verdict_dict["verdict"],
                    "confidence": verdict_dict["confidence"],
                    "primary_driver": verdict_dict["primary_driver"],
                    "flags": verdict_dict["flags"],
                    "reasoning": verdict_dict["reasoning"],
                },
                "news_report": {"summary": "No news fetch in watcher mode "
                                            "— see Sheets log for full pipeline alerts.",
                                "has_news": False},
                "event_context": get_event_context(sym, coin, price),
                "technical_outlook": get_technical_outlook(coin, price),
            }

            if send_alert_if_enabled(alert):
                last_alert_ts[sym] = time.time()
                print(f"[t{tick_n}] 🔔 {sym} {move_pct:+.2f}% → sent "
                      f"({verdict_dict['condition_id']} {verdict_dict['confidence']})",
                      flush=True)

            prev_oi[sym] = float(ctx.get("openInterest") or 0)

        remaining = int(args.timeout - (time.time() - start))
        print(f"[t{tick_n}] tick done · {remaining}s left", flush=True)
        time.sleep(POLL_SEC)

    send_message(f"<b>🛰️ Live watcher exited after {args.timeout}s.</b> "
                  f"Alerts this session: "
                  f"<code>{sum(1 for v in last_alert_ts.values() if v > 0)}</code>")


if __name__ == "__main__":
    main()
