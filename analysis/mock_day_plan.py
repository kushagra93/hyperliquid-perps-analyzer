#!/usr/bin/env python3
"""
analysis/mock_day_plan.py
────────────────────────────────────────────────────────────────────
Generate the FULL DAY of push-notification + ambient channel posts
for a given calendar date — using yesterday's actual HL signals as
test fodder.

Two streams are interleaved chronologically:

  • Ambient daypart anchors (NOT push notifications) — short
    lock-screen-worthy lines tied to Indian retail rhythms
    (morning chai, India close, US pre-open, power hour, etc.).
  • Signal-driven alerts — re-using the brand-voice copywriter
    with multi-variant generation. The single highest-scoring
    eligible signal of the day is flagged 🏆 PN OF THE DAY
    (the 1-PN-per-day budget).

Usage:
  # Default = yesterday (IST)
  python3 analysis/mock_day_plan.py
  python3 analysis/mock_day_plan.py --date 2026-04-29 --telegram
  python3 analysis/mock_day_plan.py --date 2026-04-29 --include-variants
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.historical_report import run as scan_history  # noqa: E402
from notifiers.pn_copywriter import (   # noqa: E402
    DAYPARTS, daypart_copy_for, activity_hooks_for,
    generate_pn_variants,
)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Helpers ──────────────────────────────────────────────────────

def _ist_dt(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(IST)


def _signals_on(d: date) -> list[dict]:
    """Pull recent signals and filter to those whose IST date == d."""
    # Scan a window large enough to surely include date d
    today_ist = datetime.now(IST).date()
    days = max(7, (today_ist - d).days + 5)
    rep = scan_history(days)
    out = []
    for s in rep["signals"]:
        try:
            sdt = _ist_dt(s["when_ist"])
            if sdt.date() == d:
                out.append(s)
        except Exception:
            continue
    return out, rep


def _alert_dict_from_signal(s: dict) -> dict:
    """Adapt a signal-from-report into the alert shape pn_copywriter expects."""
    return {
        "symbol": s["ticker"],
        "full_name": s.get("full_name", s["ticker"]),
        "price_trigger": {
            "current_price": s["price"],
            "price_change_pct": s["move_pct"],
            "trigger_source": "price",
            "triggered_at": s["when_ist"],
        },
        "oi_report": {"oi_change_pct": 0.0, "funding_rate": 0.0},
        "condition": {
            "condition_id": s["condition_id"],
            "label": "Strong bull" if s["condition_id"] == "C1" else "Strong bear",
            "description": "—",
            "oi_change_pct": 0.0,
        },
        "causality": {"verdict": "—", "confidence": "high",
                       "primary_driver": "oi_flow", "flags": [], "reasoning": "—"},
        "news_report": {"summary": "—", "has_news": False},
        "event_context": {"enabled": False},
        "technical_outlook": {"enabled": False, "atr": s.get("atr") or 0.0},
        "score": s["score"],
        "stars": s["stars"],
        "pn_today": s.get("pn_today", False),
    }


def _hour_seq(d: date) -> list[datetime]:
    """Generate one IST datetime per daypart bucket spanning the day."""
    out = []
    for start, end, label, variants in DAYPARTS:
        if not variants:
            continue
        h = (start + end) // 2
        out.append(datetime(d.year, d.month, d.day, h, 0, tzinfo=IST))
    return out


# ── Plan builder ─────────────────────────────────────────────────

def build_plan(d: date, *, include_variants: bool = False) -> dict:
    signals, full_report = _signals_on(d)
    print(f"  [info] {len(signals)} signals on {d.isoformat()}", flush=True)

    # Activity hooks (whole-day)
    activities = activity_hooks_for(d)

    # PN-of-the-day = highest-scoring signal that day
    pn_pick = None
    if signals:
        pn_pick = max(signals, key=lambda s: s["score"])

    # Build chronological items
    items: list[dict] = []

    # Activity banner at start of day if present
    for a_line in activities:
        items.append({
            "kind": "activity",
            "ts": datetime(d.year, d.month, d.day, 7, 0, tzinfo=IST).isoformat(),
            "label": "context",
            "headline": a_line,
            "is_pn": False,
        })

    # Daypart anchors
    for ts in _hour_seq(d):
        dp = daypart_copy_for(ts)
        if not dp:
            continue
        items.append({
            "kind": "daypart",
            "ts": ts.isoformat(),
            "label": dp["label"],
            "headline": dp["headline"],
            "is_pn": False,
        })

    # Signal-driven entries
    for s in signals:
        alert = _alert_dict_from_signal(s)
        is_pn = pn_pick is not None and s["ts_ms"] == pn_pick["ts_ms"]
        alert["pn_today"] = is_pn
        variants = generate_pn_variants(alert, n=3 if include_variants else 1)
        items.append({
            "kind": "signal",
            "ts": s["when_ist"],
            "ticker": s["ticker"],
            "condition": s["condition_id"],
            "move_pct": s["move_pct"],
            "score": s["score"],
            "stars": s["stars"],
            "outcome": s.get("outcome"),
            "pnl_pct": s.get("pnl_pct"),
            "is_pn": is_pn,
            "variants": variants,
        })

    items.sort(key=lambda i: i["ts"])
    return {
        "date": d.isoformat(),
        "tickers_in_universe": full_report["tickers_scanned"],
        "signal_count": len(signals),
        "pn_pick": pn_pick["ticker"] + "/" + pn_pick["condition_id"] if pn_pick else None,
        "items": items,
    }


# ── Renderers ────────────────────────────────────────────────────

def render_console(plan: dict) -> str:
    lines = [f"\n═══ MOCK PN PLAN for {plan['date']} ═══"]
    lines.append(f"Universe: {len(plan['tickers_in_universe'])} tickers · "
                  f"signals fired: {plan['signal_count']} · "
                  f"PN of day: {plan['pn_pick'] or '(none)'}\n")
    for it in plan["items"]:
        ts = _ist_dt(it["ts"]).strftime("%H:%M")
        if it["kind"] == "signal":
            tag = "🏆 PN" if it["is_pn"] else "📡 alert"
            head = it["variants"][0]["headline"]
            lines.append(f"  {ts}  {tag:9s}  {head}")
            if len(it["variants"]) > 1:
                for v in it["variants"][1:]:
                    lines.append(f"         alt        {v['headline']}")
            lines.append(f"           outcome={it['outcome']}  pnl={it.get('pnl_pct')}")
        else:
            tag = "🌐 ambient" if it["kind"] == "daypart" else "🎯 context"
            lines.append(f"  {ts}  {tag:9s}  {it['headline']}")
    return "\n".join(lines)


def render_telegram(plan: dict) -> list[str]:
    msgs: list[str] = []
    head = (
        f"<b>📅 MOCK PN PLAN — {plan['date']}</b>\n"
        f"<i>What a full day looked like (or would look like) for an "
        f"Indian US-stock trader on this date. Daypart anchors are "
        f"channel posts; signal cards are PN-tier candidates; "
        f"highest score becomes 🏆 PN OF THE DAY.</i>\n\n"
        f"Universe: {len(plan['tickers_in_universe'])} tickers · "
        f"signals: {plan['signal_count']} · "
        f"PN: <b>{plan['pn_pick'] or '—'}</b>"
    )
    msgs.append(head)

    cur_block: list[str] = []

    def flush():
        if cur_block:
            msgs.append("\n".join(cur_block))

    for it in plan["items"]:
        ts = _ist_dt(it["ts"]).strftime("%H:%M IST")
        if it["kind"] == "signal":
            tag = "🏆 <b>PN OF THE DAY</b>" if it["is_pn"] else "📡 <b>signal</b>"
            outcome = it.get("outcome") or "?"
            pnl = it.get("pnl_pct")
            pnl_s = f"{pnl:+.2f}%" if pnl is not None else "n/a"
            outcome_emoji = {"tp1": "🟢 TP1", "sl": "🔴 SL",
                              "timeout": "⚪ timeout"}.get(outcome, outcome)
            block = [f"<b>━━━ {ts} ━━━</b>", f"{tag} · {it['ticker']} {it['condition']} "
                     f"{it['move_pct']:+.2f}% · score {it['score']}/100 · ⭐×{it['stars']}"]
            for i, v in enumerate(it["variants"], 1):
                label = "Variant" if len(it["variants"]) > 1 else "Headline"
                block.append(f"<b>{label} {i}:</b> {v['headline']}")
                if v.get("body"):
                    block.append(v["body"])
            block.append(f"<b>Outcome:</b> {outcome_emoji} ({pnl_s})")
            msgs.append("\n".join(block))
        else:
            tag = "🌐 <i>ambient</i>" if it["kind"] == "daypart" else "🎯 <i>context</i>"
            cur_block.append(f"<b>{ts}</b>  {tag}  —  {it['headline']}")
            if len("\n".join(cur_block)) > 3000:
                flush(); cur_block = []
    flush()
    return msgs


# ── Driver ───────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (IST). Default = yesterday.")
    p.add_argument("--include-variants", action="store_true",
                   help="emit 3 PN variants per signal (default 1)")
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--out", default=None,
                   help="write plan JSON to this path")
    args = p.parse_args()

    if args.date:
        d = date.fromisoformat(args.date)
    else:
        d = (datetime.now(IST) - timedelta(days=1)).date()

    plan = build_plan(d, include_variants=args.include_variants)
    print(render_console(plan))

    if args.out:
        Path(args.out).write_text(json.dumps(plan, indent=2))
        print(f"\nWrote plan JSON to {args.out}")

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN",
            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        for m in render_telegram(plan):
            subprocess.run([
                "curl", "-s", "-X", "POST",
                f"https://api.telegram.org/bot{token}/sendMessage",
                "--data-urlencode", f"chat_id={chat}",
                "--data-urlencode", f"text={m}",
                "--data-urlencode", "parse_mode=HTML",
                "--data-urlencode", "disable_web_page_preview=true",
            ], capture_output=True, text=True, timeout=15)
            time.sleep(0.4)


if __name__ == "__main__":
    main()
