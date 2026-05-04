#!/usr/bin/env python3
"""
analysis/system_report.py
────────────────────────────────────────────────────────────────────
Comprehensive system performance report — every alert across every
ticker, win rate, total PnL, per-asset and per-condition breakdowns,
best / worst trades, PN-tier subset, and a recent-session view.

Pushes a multi-message Telegram thread for in-channel consumption.
Reuses analysis/historical_report.run() so numbers always match the
dashboard.

Usage:
  python3 analysis/system_report.py --days 60 --telegram
  python3 analysis/system_report.py --days 30 --no-telegram   # stdout only
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.historical_report import run as scan_history  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _send(text: str, token: str, chat: str) -> None:
    subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "--data-urlencode", f"chat_id={chat}",
        "--data-urlencode", f"text={text}",
        "--data-urlencode", "parse_mode=HTML",
        "--data-urlencode", "disable_web_page_preview=true",
    ], capture_output=True, text=True, timeout=15)


def _emoji_outcome(o: str) -> str:
    return {"tp1": "🟢", "sl": "🔴", "timeout": "⚪", "open": "⏳", "no_atr": "—"}.get(o, "?")


def _format_big_int(n: int) -> str:
    return f"{n:,}"


def build_messages(report: dict) -> list[str]:
    a = report["aggregate"]
    pn = report.get("pn_aggregate") or {"n": 0, "wins": 0, "win_rate_pct": 0, "avg_pnl_pct": 0}
    pn_filt = report.get("pn_filter") or {"applied": False}
    sigs = report["signals"]

    closed = [s for s in sigs if s.get("outcome") in ("tp1", "sl", "timeout")]
    open_ = [s for s in sigs if s.get("outcome") == "open"]
    wins = [s for s in closed if (s.get("pnl_pct") or 0) > 0]
    losses = [s for s in closed if (s.get("pnl_pct") or 0) <= 0]
    total_pnl = sum((s.get("pnl_pct") or 0) for s in closed)
    avg_win = sum((s.get("pnl_pct") or 0) for s in wins) / len(wins) if wins else 0.0
    avg_loss = sum((s.get("pnl_pct") or 0) for s in losses) / len(losses) if losses else 0.0
    expectancy = (a["win_rate_pct"]/100) * avg_win + (1 - a["win_rate_pct"]/100) * avg_loss

    msgs: list[str] = []

    # ─── 1. Header KPIs ───
    msgs.append(
        f"<b>📊 SYSTEM REPORT — {report['scan_days']}d</b>\n"
        f"<i>{report['as_of']}</i>\n\n"
        f"<b>Universe:</b> {len(report['tickers_scanned'])} tickers · "
        f"<b>Signals:</b> {a['total_signals']} · "
        f"<b>Closed:</b> {a['closed']} · "
        f"<b>Open:</b> {a['open']}\n\n"
        f"<b>Headline performance</b>\n"
        f"  • Win rate: <b>{a['wins']}/{a['closed']} = {a['win_rate_pct']}%</b>\n"
        f"  • Total PnL: <b>{total_pnl:+.2f}%</b>\n"
        f"  • Avg PnL/signal: <b>{a['avg_pnl_pct']:+}%</b>\n"
        f"  • Avg win: <b>{avg_win:+.2f}%</b>  ·  Avg loss: <b>{avg_loss:+.2f}%</b>\n"
        f"  • Expectancy: <b>{expectancy:+.3f}%/signal</b>\n"
        f"<i>(TP1 = +2×ATR, SL = −1.5×ATR, 24-bar / 6h timeout)</i>"
    )

    # ─── 2. PN tier ───
    if pn_filt.get("applied"):
        msgs.append(
            f"<b>🔔 PN TIER — daily 1-PN budget</b>\n"
            f"<i>filter: <code>{' &amp; '.join(pn_filt['filter'])}</code></i>\n\n"
            f"  • PNs delivered: <b>{pn['n']}</b> "
            f"({pn['n']/report['scan_days']:.2f}/day)\n"
            f"  • Win rate: <b>{pn.get('wins',0)}/{pn['closed']} = {pn['win_rate_pct']}%</b>\n"
            f"  • Avg PnL: <b>{pn['avg_pnl_pct']:+}%</b>\n"
            f"<i>The optimizer-selected filter on top of the universe.</i>"
        )

    # ─── 3. Per-condition ───
    cond_lines = ["<b>📋 By condition</b>", "<pre>"]
    cond_lines.append(f"{'Cond':4s} {'n':>4s} {'win %':>6s} {'avg PnL':>9s}")
    for k in sorted(a["by_condition"]):
        v = a["by_condition"][k]
        cond_lines.append(f"{k:4s} {v['n']:>4d} {v['win_rate_pct']:>5.1f}% {v['avg_pnl_pct']:>+8.2f}%")
    cond_lines.append("</pre>")
    msgs.append("\n".join(cond_lines))

    # ─── 4. Per-asset (most active first) ───
    by_t = a["by_ticker"]
    sorted_tickers = sorted(by_t.keys(), key=lambda x: -by_t[x]["n"])
    asset_lines = ["<b>📈 By asset</b> (active → quiet)", "<pre>"]
    asset_lines.append(f"{'Ticker':7s} {'n':>4s} {'win %':>6s} {'avg PnL':>9s} {'tot PnL':>9s}")
    for k in sorted_tickers:
        v = by_t[k]
        # Sum PnL for this ticker
        tot = sum((s.get("pnl_pct") or 0) for s in closed if s["ticker"] == k)
        asset_lines.append(f"{k:7s} {v['n']:>4d} {v['win_rate_pct']:>5.1f}% "
                            f"{v['avg_pnl_pct']:>+8.2f}% {tot:>+8.2f}%")
    asset_lines.append("</pre>")
    msgs.append("\n".join(asset_lines))

    # ─── 5. Top 8 winners ───
    winners = sorted(closed, key=lambda s: -(s.get("pnl_pct") or 0))[:8]
    if winners:
        lines = ["<b>🏆 Best 8 trades</b>"]
        for s in winners:
            t = s["when_ist"].replace("T", " ")[:16]
            lines.append(
                f"  {_emoji_outcome(s['outcome'])} <b>{s['ticker']:5}</b> {t} IST · "
                f"{s['condition_id']} {s['move_pct']:+.2f}% → "
                f"<b>{s.get('pnl_pct'):+.2f}%</b>"
            )
        msgs.append("\n".join(lines))

    # ─── 6. Worst 5 losers ───
    losers = sorted(closed, key=lambda s: (s.get("pnl_pct") or 0))[:5]
    if losers and any((s.get("pnl_pct") or 0) < 0 for s in losers):
        lines = ["<b>💀 Worst 5 trades</b>"]
        for s in losers:
            if (s.get("pnl_pct") or 0) >= 0:
                continue
            t = s["when_ist"].replace("T", " ")[:16]
            lines.append(
                f"  {_emoji_outcome(s['outcome'])} <b>{s['ticker']:5}</b> {t} IST · "
                f"{s['condition_id']} {s['move_pct']:+.2f}% → "
                f"<b>{s.get('pnl_pct'):+.2f}%</b>"
            )
        msgs.append("\n".join(lines))

    # ─── 7. Recent 7d activity ───
    cutoff = datetime.now(IST) - timedelta(days=7)
    recent = [s for s in sigs if datetime.fromisoformat(s["when_ist"]).astimezone(IST) >= cutoff]
    if recent:
        recent_closed = [s for s in recent if s.get("outcome") in ("tp1", "sl", "timeout")]
        recent_wins = [s for s in recent_closed if (s.get("pnl_pct") or 0) > 0]
        recent_pnl = sum((s.get("pnl_pct") or 0) for s in recent_closed)
        wr = round(len(recent_wins) / len(recent_closed) * 100, 1) if recent_closed else 0.0
        lines = [
            f"<b>⏱ Last 7 days</b>",
            f"  • Signals: <b>{len(recent)}</b>  ·  resolved: {len(recent_closed)}",
            f"  • Win rate: <b>{wr}%</b>  ·  PnL sum: <b>{recent_pnl:+.2f}%</b>",
            "",
            "<i>Most recent 12:</i>",
        ]
        for s in recent[:12]:
            t = s["when_ist"].replace("T", " ")[:16]
            pnl = s.get("pnl_pct")
            pnl_s = f"{pnl:+.2f}%" if pnl is not None else "—"
            lines.append(
                f"  {_emoji_outcome(s['outcome'])} {t}  <b>{s['ticker']:5}</b>  "
                f"{s['condition_id']} {s['move_pct']:+.2f}% → {pnl_s}"
            )
        msgs.append("\n".join(lines))

    # ─── 8. Open positions ───
    if open_:
        lines = [f"<b>⏳ {len(open_)} open positions (resolution pending)</b>"]
        for s in open_[:8]:
            t = s["when_ist"].replace("T", " ")[:16]
            lines.append(
                f"  {t}  <b>{s['ticker']:5}</b>  {s['condition_id']} "
                f"{s['move_pct']:+.2f}%  score {s['score']}"
            )
        msgs.append("\n".join(lines))

    msgs.append(
        "<i>Caveats: HL candleSnapshot does not expose OI history → condition "
        "assignment in this report is price-only, a noisier subset of the "
        "production OI-gated signal. Live workers using main.py with full OI "
        "history will fire fewer alerts at higher quality.</i>"
    )

    return msgs


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--no-telegram", action="store_true")
    args = p.parse_args()

    print(f"Scanning {args.days}d...", flush=True)
    report = scan_history(args.days)

    out_dir = ROOT / "analysis"
    (out_dir / "report.json").write_text(json.dumps(report, indent=2))
    pub = ROOT / "dashboard" / "report.json"
    pub.write_text(json.dumps(report))
    pub2 = ROOT / "dashboard" / "public" / "report.json"
    pub2.parent.mkdir(parents=True, exist_ok=True)
    pub2.write_text(json.dumps(report))

    msgs = build_messages(report)
    print(f"\nBuilt {len(msgs)} messages, {sum(len(m) for m in msgs)} chars total\n")
    for m in msgs:
        print("─" * 70)
        print(m[:400] + ("…" if len(m) > 400 else ""))

    if not args.no_telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN",
            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        print(f"\nPushing {len(msgs)} messages to Telegram...")
        for m in msgs:
            _send(m, token, chat)
            time.sleep(0.5)
        print("done.")


if __name__ == "__main__":
    main()
