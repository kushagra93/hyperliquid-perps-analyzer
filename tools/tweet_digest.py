#!/usr/bin/env python3
"""
tools/tweet_digest.py
────────────────────────────────────────────────────────────────────
Tweet-style digest of trigger activity. Three sections:

  • PAST 2 DAYS — historical signals from analysis/report.json
  • TODAY       — fires from eval/realtime_pn.jsonl with today's IST date
  • LIVE NOW    — current top movers (24h + 30m) from compute_movers

Each fire is formatted as a tweet ≤ 280 chars:
  $NVDA 🔴 24h -1.5% · 30m -0.8% · tariff news, vol 4× · sell zone

Pushes to Telegram as a multi-message thread; also prints to stdout.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.top_movers import compute_movers, classify_condition
from analysis.system_v2 import cluster_label

IST = timezone(timedelta(hours=5, minutes=30))
TWEET_MAX = 280


def _truncate(s: str, n: int = TWEET_MAX) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip(" ,.;:") + "…"


def _emoji(move: float) -> str:
    return "🔴" if move < -0.3 else "🟢" if move > 0.3 else "⚪"


# ── Tweet builders ───────────────────────────────────────────────

def tweet_from_pn_fire(rec: dict) -> str:
    """JSONL fire (from realtime service) → tweet."""
    kind = rec.get("kind", "?")
    title = rec.get("title", "").replace("<b>", "").replace("</b>", "")
    body  = rec.get("body", "")
    # Strip the 📰/📈 prefixes from body
    body_compact = body.replace("📰 ", "").replace("📈 ", "")
    body_compact = body_compact.replace("\n", " · ")
    when_iso = rec.get("ts_ist") or rec.get("ts_utc", "")
    when = when_iso[:16].replace("T", " ") if when_iso else ""
    # Add #hashtag
    tag_map = {
        "volume_spike": "#VolumeSpike",
        "breakout":     "#Breakout",
        "cluster_shift":"#RiskOff",
        "sentiment":    "#NewsAlert",
        "recap":        "#DailyRecap",
        "tier_confirm": "",
    }
    tag = tag_map.get(kind, "")
    parts = [title, body_compact]
    if when: parts.append(when)
    if tag:  parts.append(tag)
    return _truncate(" · ".join(p for p in parts if p))


def tweet_from_signal(s: dict) -> str:
    """Historical signal (analysis/report.json) → tweet."""
    sym = s["ticker"]
    cid = s.get("condition_id", "?")
    move = s.get("move_pct", 0)
    outcome = s.get("outcome", "")
    pnl = s.get("pnl_pct")
    when = s.get("when_ist", "")[:16].replace("T", " ")
    emoji = _emoji(move)
    out_emoji = {"tp1": "🟢 TP", "sl": "🔴 SL", "timeout": "⚪ to"}.get(outcome, "")
    pnl_s = f" · {pnl:+.2f}% PnL" if pnl is not None else ""
    return _truncate(
        f"${sym} {emoji} {move:+.2f}% · {cid} signal{pnl_s} → {out_emoji} · "
        f"{when} #SignalLog"
    )


def tweet_from_live_row(r: dict) -> str:
    """compute_movers row → live tweet."""
    sym = r["symbol"]
    m_24h = r.get("move_24h_pct", 0)
    m_30m = r["move_pct"]
    group = cluster_label(r.get("cluster", ""))
    cond = classify_condition(m_30m, r.get("funding", 0))
    cond_label = {
        "C1": "fresh buyers in", "C2": "fresh sellers in",
        "C3": "buyers exiting", "C4": "sellers covering",
        "FLAT": "drifting",
    }.get(cond, "")
    emoji = _emoji(m_30m)
    return _truncate(
        f"${sym} {emoji} 24h {m_24h:+.1f}% · 30m {m_30m:+.1f}% · "
        f"{cond_label} · {group} #LiveTape"
    )


# ── Source readers ───────────────────────────────────────────────

def read_pn_fires(jsonl_path: Path, since_utc: datetime) -> list[dict]:
    if not jsonl_path.exists():
        return []
    out = []
    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("kind") == "tier_confirm":
            continue
        ts = rec.get("ts_utc")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            continue
        if dt < since_utc:
            continue
        rec["_dt"] = dt
        out.append(rec)
    return out


def read_historical_signals(report_path: Path,
                              since_ist: datetime) -> list[dict]:
    if not report_path.exists():
        return []
    rep = json.loads(report_path.read_text())
    out = []
    for s in rep.get("signals", []):
        ts = s.get("when_ist")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
        except Exception:
            continue
        if dt.astimezone(IST) < since_ist:
            continue
        s["_dt"] = dt
        out.append(s)
    return out


# ── Send ─────────────────────────────────────────────────────────

def _tg_send(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN",
                            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
    chat = os.environ.get("TELEGRAM_PN_CHANNEL_ID") \
        or os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
    subprocess.run([
        "curl", "-s", "-X", "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        "--data-urlencode", f"chat_id={chat}",
        "--data-urlencode", f"text={text}",
        "--data-urlencode", "parse_mode=HTML",
        "--data-urlencode", "disable_web_page_preview=true",
    ], capture_output=True, text=True, timeout=10)


# ── Driver ───────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--max-per-section", type=int, default=8)
    p.add_argument("--telegram", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    now_utc = datetime.now(timezone.utc)
    today_ist = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_ist = today_ist - timedelta(days=1)
    two_days_ago_ist = today_ist - timedelta(days=2)

    pn_path = ROOT / "eval" / "realtime_pn.jsonl"
    rep_path = ROOT / "analysis" / "report.json"

    # ── PAST 2 DAYS ──
    past_pn = read_pn_fires(pn_path,
                              today_ist.astimezone(timezone.utc) - timedelta(days=2))
    past_pn = [r for r in past_pn if r["_dt"].astimezone(IST) < today_ist]
    past_signals = read_historical_signals(rep_path, two_days_ago_ist)
    past_signals = [s for s in past_signals if s["_dt"].astimezone(IST) < today_ist]

    past_tweets: list[str] = []
    for r in sorted(past_pn, key=lambda x: -x["_dt"].timestamp())[:args.max_per_section // 2]:
        past_tweets.append(tweet_from_pn_fire(r))
    for s in sorted(past_signals, key=lambda x: -x["_dt"].timestamp())[:args.max_per_section]:
        past_tweets.append(tweet_from_signal(s))
    past_tweets = past_tweets[: args.max_per_section]

    # ── TODAY ──
    today_pn = read_pn_fires(pn_path,
                               today_ist.astimezone(timezone.utc))
    today_signals = read_historical_signals(rep_path, today_ist)
    today_tweets: list[str] = []
    for r in sorted(today_pn, key=lambda x: -x["_dt"].timestamp())[:args.max_per_section]:
        today_tweets.append(tweet_from_pn_fire(r))
    for s in sorted(today_signals, key=lambda x: -x["_dt"].timestamp())[:args.max_per_section]:
        today_tweets.append(tweet_from_signal(s))
    today_tweets = today_tweets[: args.max_per_section]

    # ── LIVE NOW ──
    rows = compute_movers(window_min=30, universe_mode="all", focus="all")
    rows.sort(key=lambda r: -(abs(r.get("move_24h_pct", 0)) + abs(r["move_pct"])))
    live_tweets = [tweet_from_live_row(r) for r in rows[:args.max_per_section]]

    # ── Compose Telegram thread ──
    sections = [
        ("📅 <b>PAST 2 DAYS — triggers fired</b>", past_tweets),
        ("☀️ <b>TODAY — triggers so far</b>", today_tweets),
        ("📡 <b>LIVE NOW — current top movers</b>", live_tweets),
    ]

    print("\n═══ TWEET DIGEST ═══")
    for header, tweets in sections:
        print(f"\n{header.replace('<b>','').replace('</b>','')}")
        if not tweets:
            print("  (no entries)")
            continue
        for t in tweets:
            print(f"  {t}")

    if args.telegram or not args.dry_run:
        for header, tweets in sections:
            block = [header]
            if not tweets:
                block.append("<i>(no entries in this window)</i>")
            else:
                for t in tweets:
                    # Telegram escape — already plain text, leave as-is
                    block.append(f"• {t}")
            _tg_send("\n".join(block))
            time.sleep(0.4)
        print("\nPushed to Telegram.")


if __name__ == "__main__":
    main()
