#!/usr/bin/env python3
"""
events/preview.py
────────────────────────────────────────────────────────────────────
CLI to inspect the event calendar for one or more tickers without
spinning up the full TickerWorker loop.

Usage:
  python3 events/preview.py NVDA TSLA
  python3 events/preview.py --all                # every ticker in config
  python3 events/preview.py --macro              # just the macro calendar
  python3 events/preview.py NVDA --days 30       # custom lookahead
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from events.fetcher import get_earnings_calendar, get_macro_calendar, upcoming_for_symbol
from events.expected_move import expected_move_for_event
from config.tickers import TICKERS


def _last_price(coin: str) -> float:
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "15m",
        "startTime": int(time.time() * 1000) - 6 * 3600 * 1000,
        "endTime": int(time.time() * 1000),
    }}
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=10,
        )
        arr = json.loads(r.stdout)
        return float(arr[-1]["c"]) if arr else 0.0
    except Exception:
        return 0.0


def preview_symbol(symbol: str, days: int) -> None:
    cfg = TICKERS.get(symbol, {})
    coin = cfg.get("hl_asset", f"xyz:{symbol}")
    bundle = upcoming_for_symbol(symbol, days=days)
    print(f"\n═══ {symbol} ═══")
    ner = bundle.get("next_earnings")
    if ner:
        print(f"  Next earnings: {ner['date']} ({ner.get('hour') or '?'})  "
              f"EPS est={ner.get('eps_estimate')}  "
              f"in {bundle.get('days_to_earnings')}d")
    else:
        print("  No earnings in window.")

    hist = bundle.get("earnings_history") or []
    if hist:
        print("  Earnings history:")
        for h in hist:
            print(f"    {h['period']:10s}  actual={h.get('eps_actual'):>6}  "
                  f"est={h.get('eps_estimate'):>6}  surprise={h.get('surprise_pct')}")

    if ner and bundle.get("days_to_earnings") is not None:
        price = _last_price(coin)
        em = expected_move_for_event(coin, price, bundle["days_to_earnings"], hist)
        print(f"  Expected move (±): {em['expected_pct']:.2f}%  "
              f"band ${em['lower_band']:.2f} – ${em['upper_band']:.2f}")
        print(f"    statistical: {em['statistical_pct']:.2f}%  "
              f"historical: {em.get('historical_earnings_pct')}%  "
              f"(n={em['historical_n']} quarters)")


def preview_macro(days: int) -> None:
    rows = get_macro_calendar(days)
    print(f"\n═══ Macro — next {days}d ({len(rows)} events) ═══")
    for r in rows[:40]:
        print(f"  {r.get('time','')[:16]}  {r.get('country',''):3s}  {r.get('event','')}  "
              f"prev={r.get('prev')} est={r.get('estimate')}")


def preview_monthly(tickers: list[str], days: int, to_telegram: bool) -> None:
    """
    Monthly review:
      1. Past earnings reported in the last `days` days — actual vs estimate,
         post-earnings price move from HL candles.
      2. Upcoming earnings in the next `days` days (YET TO RELEASE) —
         rule-based directional lean from four deterministic signals.
    """
    from events.monthly_review import monthly_report, LEAN_LABELS

    report = monthly_report(tickers, days)
    past = report["past_earnings"]
    upcoming = report["upcoming_forecasts"]

    # ── stdout ──
    print(f"\n═══ MONTHLY EARNINGS REVIEW ({days}d window) ═══")
    print(f"As of: {report['as_of']}")
    print(f"Tickers: {len(tickers)}  •  past: {len(past)}  •  upcoming: {len(upcoming)}\n")

    print("── PAST EARNINGS (already reported) ──")
    if not past:
        print("  none in window")
    for r in past:
        move = r.get("post_earnings_move_pct")
        move_s = f"{move:+.2f}%" if move is not None else "n/a"
        sp = r.get("surprise_pct")
        sp_s = f"{sp:+.2f}%" if sp is not None else "n/a"
        print(f"  {r['period']}  {r['symbol']:6s}  "
              f"{r['verdict'].upper():7s}  surprise={sp_s}  "
              f"actual={r.get('eps_actual')}  est={r.get('eps_estimate')}  "
              f"T±1 move={move_s}")

    print("\n── UPCOMING (YET TO RELEASE — forecast) ──")
    if not upcoming:
        print("  none in window")
    for r in upcoming:
        print(f"\n  {r['symbol']}  →  earnings in {r.get('days_to_earnings','?')}d "
              f"({r['next_earnings_date']} {r.get('earnings_hour') or ''}) "
              f"est EPS={r.get('eps_estimate')}")
        print(f"    lean: {r['lean']}   score={r['score']:+d}/4")
        for name, sig in r["signals"].items():
            print(f"      • {name:18s}  {sig['score']:+d}  — {sig['detail']}")
        if r.get("top_headlines"):
            print("    top headlines:")
            for h in r["top_headlines"][:3]:
                print(f"      - {h[:110]}")

    if not to_telegram:
        return

    # ── Telegram ──
    import os as _os, subprocess as _sp, json as _json
    token = _os.environ.get("TELEGRAM_BOT_TOKEN",
        "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
    chat = _os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")

    def tg(text):
        _sp.run([
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "--data-urlencode", f"chat_id={chat}",
            "--data-urlencode", f"text={text}",
            "--data-urlencode", "parse_mode=HTML",
            "--data-urlencode", "disable_web_page_preview=true",
        ], capture_output=True, text=True, timeout=15)

    lines = [
        f"<b>🗓️ MONTHLY EARNINGS REVIEW — {days}d window</b>",
        f"<i>As of {report['as_of'][:16].replace('T',' ')}Z · "
        f"{len(past)} past · {len(upcoming)} upcoming</i>",
    ]
    lines.append("")
    lines.append("<b>📜 PAST EARNINGS (already reported)</b>")
    if not past:
        lines.append("  <i>none in window</i>")
    for r in past[:12]:
        move = r.get("post_earnings_move_pct")
        move_s = f"{move:+.2f}%" if move is not None else "n/a"
        sp = r.get("surprise_pct")
        sp_s = f"{sp:+.2f}%" if sp is not None else "n/a"
        verdict_emoji = {"beat": "🟢", "miss": "🔴", "in-line": "⚪"}[r["verdict"]]
        lines.append(
            f"  {verdict_emoji} <b>{r['symbol']}</b> {r['period']}  "
            f"surprise {sp_s} · T±1 {move_s}"
        )

    lines.append("")
    lines.append("<b>🔮 UPCOMING — YET TO RELEASE (rule-based lean)</b>")
    if not upcoming:
        lines.append("  <i>none in window</i>")
    for r in upcoming:
        dte = r.get("days_to_earnings")
        dte_s = f"in {dte}d" if dte is not None else "tbd"
        lines.append(
            f"\n  <b>{r['symbol']}</b> — earnings {dte_s} "
            f"({r['next_earnings_date']} {r.get('earnings_hour') or ''}) · "
            f"EPS est <code>{r.get('eps_estimate')}</code>"
        )
        lines.append(f"  <b>Lean: {r['lean']}</b>  score <code>{r['score']:+d}/4</code>")
        for name, sig in r["signals"].items():
            emoji = "🟢" if sig["score"] > 0 else "🔴" if sig["score"] < 0 else "⚪"
            label = name.replace("_", " ")
            lines.append(f"    {emoji} {label}: <i>{sig['detail']}</i>")
        if r.get("top_headlines"):
            lines.append("  <i>top headlines:</i>")
            for h in r["top_headlines"][:2]:
                lines.append(f"    • {h[:110]}")

    lines.append("")
    lines.append(
        "<i>Signals: beat/miss streak · analyst recs · price-target vs spot · "
        "news keyword sentiment. No LLM inference — all rule-based from Finnhub + HL candles.</i>"
    )

    # Split into ≤ 4000-char Telegram messages
    text = "\n".join(lines)
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur); cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur: chunks.append(cur)
    for ch in chunks:
        tg(ch)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("symbols", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--macro", action="store_true")
    p.add_argument("--monthly", action="store_true",
                   help="monthly earnings review + forecast for upcoming releases")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--telegram", action="store_true",
                   help="also push monthly report to Telegram")
    args = p.parse_args()

    if args.macro:
        preview_macro(args.days)
        return

    if args.monthly:
        syms = args.symbols or list(TICKERS.keys())
        syms = [s.upper() for s in syms]
        preview_monthly(syms, args.days, args.telegram)
        return

    syms = args.symbols or (list(TICKERS.keys()) if args.all else ["NVDA"])
    for s in syms:
        preview_symbol(s.upper(), args.days)


if __name__ == "__main__":
    main()
