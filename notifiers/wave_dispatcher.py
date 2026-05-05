# notifiers/wave_dispatcher.py
# ─────────────────────────────────────────────────────────────────
# Schedule the 3-stage compact PN wave for a single market event.
#
# Stage 1 (break) fires immediately.
# Stage 2 (cascade) fires at T+15 min via threading.Timer.
# Stage 3 (repricing) fires at T+2 hr via threading.Timer.
#
# Re-resolves real-time data at fire time so each stage carries
# the latest move, level, and liquidation context — not a snapshot
# from when stage 1 was queued.
#
# Channel routing:
#   • Default uses TELEGRAM_PN_CHANNEL_ID env var (separate
#     dedicated PN channel — clean signal, no noise).
#   • Falls back to TELEGRAM_CHAT_ID if PN channel not set.
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from notifiers.pn_compact import (
    format_break_news, format_cascade, format_repricing,
    build_wave, to_telegram,
)

logger = logging.getLogger(__name__)

DELAY_CASCADE_SEC = 15 * 60      # T+15 min
DELAY_REPRICING_SEC = 2 * 3600   # T+2 hr

_API = "https://api.telegram.org/bot{token}/{method}"


def _channel_id() -> str | None:
    return (os.environ.get("TELEGRAM_PN_CHANNEL_ID")
            or os.environ.get("TELEGRAM_CHAT_ID"))


def _send(card_html: str) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = _channel_id()
    if not token or not chat:
        logger.warning("[wave] TELEGRAM_BOT_TOKEN / channel not set")
        return False
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST",
             _API.format(token=token, method="sendMessage"),
             "--data-urlencode", f"chat_id={chat}",
             "--data-urlencode", f"text={card_html}",
             "--data-urlencode", "parse_mode=HTML",
             "--data-urlencode", "disable_web_page_preview=true"],
            capture_output=True, text=True, timeout=15,
        )
        return '"ok":true' in r.stdout
    except Exception as e:
        logger.warning(f"[wave] send failed: {e}")
        return False


# ── Live re-resolve helpers ─────────────────────────────────────

def _hl_last_price(coin: str) -> float | None:
    payload = (
        '{"type":"candleSnapshot","req":{"coin":"' + coin
        + '","interval":"15m","startTime":' + str(int(time.time()*1000) - 6*3600*1000)
        + ',"endTime":' + str(int(time.time()*1000)) + '}}'
    )
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json",
             "-d", payload],
            capture_output=True, text=True, timeout=10,
        )
        import json
        arr = json.loads(r.stdout)
        if isinstance(arr, list) and arr:
            return float(arr[-1]["c"])
    except Exception:
        pass
    return None


def _live_move_pct(coin: str, ref_price: float) -> float:
    p = _hl_last_price(coin)
    if not p or ref_price <= 0:
        return 0.0
    return (p - ref_price) / ref_price * 100


# ── Public API ──────────────────────────────────────────────────

class WaveContext:
    """Holds the data needed to re-resolve cascade/repricing stages live."""
    def __init__(self, *, symbol: str, hl_asset: str, ref_price: float,
                  catalyst: str | None = None, level: float | None = None,
                  next_event: str | None = None,
                  liquidation_usd_m: float | None = None):
        self.symbol = symbol
        self.coin = hl_asset
        self.ref_price = ref_price
        self.catalyst = catalyst
        self.level = level
        self.next_event = next_event
        self.liquidation_usd_m = liquidation_usd_m
        self.fired_at = datetime.now(timezone.utc)


def _fire_stage_break(ctx: WaveContext, move_pct: float) -> None:
    card = format_break_news(symbol=ctx.symbol, move_pct=move_pct,
                              catalyst=ctx.catalyst)
    if _send(to_telegram(card)):
        logger.info(f"[wave/{ctx.symbol}] T+0 break: {card['title']} "
                     f"({card['total_chars']} chars)")


def _fire_stage_cascade(ctx: WaveContext, original_move_pct: float) -> None:
    live_move = _live_move_pct(ctx.coin, ctx.ref_price)
    if live_move == 0.0:
        live_move = original_move_pct  # fallback to entry move if HL fails
    card = format_cascade(symbol=ctx.symbol, move_pct=live_move,
                           liquidation_usd_m=ctx.liquidation_usd_m,
                           level=ctx.level)
    if _send(to_telegram(card)):
        logger.info(f"[wave/{ctx.symbol}] T+15 cascade: live_move {live_move:+.2f}%")


def _fire_stage_repricing(ctx: WaveContext, original_move_pct: float) -> None:
    live_move = _live_move_pct(ctx.coin, ctx.ref_price)
    if live_move == 0.0:
        live_move = original_move_pct
    card = format_repricing(symbol=ctx.symbol, next_event=ctx.next_event,
                             net_move_pct=live_move)
    if _send(to_telegram(card)):
        logger.info(f"[wave/{ctx.symbol}] T+2hr repricing: net_move {live_move:+.2f}%")


def fire_wave(*, symbol: str, hl_asset: str, ref_price: float,
               move_pct: float,
               catalyst: str | None = None,
               level: float | None = None,
               next_event: str | None = None,
               liquidation_usd_m: float | None = None) -> list[threading.Timer]:
    """
    Fire stage 1 immediately, schedule stages 2 and 3 with Timer.
    Returns the timer handles so caller can cancel if needed.
    """
    ctx = WaveContext(symbol=symbol, hl_asset=hl_asset, ref_price=ref_price,
                       catalyst=catalyst, level=level,
                       next_event=next_event,
                       liquidation_usd_m=liquidation_usd_m)

    # Stage 1: immediate
    _fire_stage_break(ctx, move_pct)

    # Stage 2: T+15
    t2 = threading.Timer(DELAY_CASCADE_SEC, _fire_stage_cascade, args=(ctx, move_pct))
    t2.daemon = True
    t2.start()

    # Stage 3: T+2 hr
    t3 = threading.Timer(DELAY_REPRICING_SEC, _fire_stage_repricing, args=(ctx, move_pct))
    t3.daemon = True
    t3.start()

    return [t2, t3]


if __name__ == "__main__":
    # Demo / smoke test
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--symbol", default="AAPL")
    p.add_argument("--move", type=float, default=-1.2)
    p.add_argument("--catalyst", default="Trump tariff 25%")
    p.add_argument("--level", type=float, default=168.0)
    p.add_argument("--next-event", default="Congress vote")
    p.add_argument("--liq", type=float, default=18.0,
                   help="liquidation in USD millions")
    p.add_argument("--demo", action="store_true",
                   help="dry-run: print all 3 stages without scheduling")
    args = p.parse_args()

    if args.demo:
        wave = build_wave(
            symbol=args.symbol, move_pct=args.move,
            catalyst=args.catalyst, level=args.level,
            liquidation_usd_m=args.liq, next_event=args.next_event,
            net_move_pct=args.move - 0.6,
        )
        for c in wave:
            print(f"\n--- {c['stage']} ({c['total_chars']} chars) ---")
            print(to_telegram(c))
    else:
        timers = fire_wave(
            symbol=args.symbol,
            hl_asset=f"xyz:{args.symbol}",
            ref_price=200.0,  # placeholder; tweak as needed
            move_pct=args.move,
            catalyst=args.catalyst, level=args.level,
            next_event=args.next_event,
            liquidation_usd_m=args.liq,
        )
        print(f"Wave queued. Stage 2 in 15min, Stage 3 in 2hr.")
        # Keep process alive for the timers
        for t in timers:
            t.join()
