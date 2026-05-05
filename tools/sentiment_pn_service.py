#!/usr/bin/env python3
"""
tools/sentiment_pn_service.py
────────────────────────────────────────────────────────────────────
Top-level sentiment-PN orchestrator. Implements the spec:

  • Layer 1 — keyword pattern → event class + baseline score
  • Layer 2 — multi-source confirmation (≥2 credible outlets, 60m)
  • Layer 3 — live price confirmation (T+30s HL poll)
  • Tier assignment with VIX-proxy + time-of-day filters
  • Wave dispatcher fires the 3-stage timeline (T+0, T+15, T+2hr)
  • Compact PN format with hard char limits + Hinglish option
  • Feedback JSONL log per fire (PN id, prediction, outcome)

Cycle every CYCLE_SEC seconds:
  1. Pull last 30 headlines from Google News (broad query)
  2. Detect events, score, tier
  3. For each TIER 1 event, pick impacted tickers, confirm price,
     fire wave via wave_dispatcher
  4. Log everything to eval/sentiment_pn.jsonl

Env:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (or TELEGRAM_PN_CHANNEL_ID)
  PN_HINGLISH=true       — switch to Hinglish action verbs
  CYCLE_SEC              — daemon poll interval (default 60)
  TIER1_ONLY=true        — only fire waves for TIER 1 events
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from events.news_search import fetch_top_headlines
from analysis.sentiment_engine import (
    EVENT_PATTERNS, detect_events_in_headlines, assign_tier,
    confirm_price_move, _vix_proxy, SentimentEvent,
)
from analysis.system_v2 import TICKER_CLUSTER
from notifiers.wave_dispatcher import fire_wave, _hl_last_price
from notifiers.compact_intel import IntelInputs, fetch_intel_signals, build_intel_body

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


# ── Broad queries that cover the event taxonomy ─────────────────
SCAN_QUERIES = [
    "Trump tariff stocks",
    "Fed rate decision",
    "CPI inflation today",
    "NFP payrolls",
    "OPEC oil supply",
    "Iran Middle East stocks",
    "Bitcoin price crash surge",
    "SEC crypto",
    "Nvidia earnings AI",
    "China trade war",
]


def _ist_now() -> datetime:
    return datetime.now(IST)


def _is_off_hours() -> bool:
    h = _ist_now().hour
    return 2 <= h < 7


def _impacted_tickers(ev: SentimentEvent) -> list[tuple[str, str]]:
    """Map event impacted_clusters into actual tickers in our universe."""
    out: list[tuple[str, str]] = []
    cluster_to_tickers: dict[str, list[str]] = {}
    for sym, cluster in TICKER_CLUSTER.items():
        cluster_to_tickers.setdefault(cluster, []).append(sym)

    for impact in ev.impacted:
        cluster, direction, _magnitude = impact
        if cluster == "idio":
            continue  # earnings/M&A — handled elsewhere
        for sym in cluster_to_tickers.get(cluster, []):
            out.append((sym, direction))
    return out


def _seen_db_path() -> Path:
    return Path(os.environ.get("SENTIMENT_SEEN_DB",
                                "eval/sentiment_seen.json"))


def _load_seen() -> dict:
    p = _seen_db_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_seen(d: dict) -> None:
    p = _seen_db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))


def _feedback_log(record: dict) -> None:
    p = Path(os.environ.get("SENTIMENT_FEEDBACK_PATH",
                              "eval/sentiment_pn.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _catalyst_for_pn(ev: SentimentEvent) -> str:
    """Short catalyst phrase for the wave title."""
    label_map = {
        "trump_tariff":      "Tariff news",
        "trade_tariff":      "Tariff escalation",
        "trade_war":         "Trade war",
        "fed_hawkish":       "Fed hawkish",
        "fed_dovish":        "Fed dovish",
        "rate_decision":     "Rate decision",
        "cpi_hot":           "CPI hot",
        "cpi_cool":          "CPI cool",
        "nfp_strong":        "NFP strong",
        "nfp_weak":          "NFP weak",
        "middle_east_war":   "ME tension",
        "opec_supply_cut":   "OPEC cut",
        "ukraine_war":       "Russia/UA",
        "earnings_beat":     "Earnings beat",
        "earnings_miss":     "Earnings miss",
        "guidance_raise":    "Guidance up",
        "guidance_cut":      "Guidance cut",
        "btc_crash":         "BTC crash",
        "btc_rally":         "BTC rally",
        "sec_approve":       "SEC approval",
        "sec_action":        "SEC action",
        "nvidia_strong":     "NVDA strong",
        "chip_export_curb":  "Chip export curb",
        "m_and_a":           "M&A news",
    }
    return label_map.get(ev.event_class, ev.event_class)


def process_event(ev: SentimentEvent, *, dry_run: bool = False) -> int:
    """
    Handle one detected event. Returns # of waves fired.
    """
    impacted = _impacted_tickers(ev)
    if not impacted:
        return 0

    # Cap to top 3 impacted tickers (avoid spam)
    impacted = impacted[:3]
    fired = 0
    cycle_id = str(uuid.uuid4())[:8]

    catalyst = _catalyst_for_pn(ev)

    for sym, direction in impacted:
        coin = f"xyz:{sym}"
        ref_price = _hl_last_price(coin) or 0
        if ref_price <= 0:
            continue

        # Layer 3: live price confirmation (T+30s)
        confirmed = confirm_price_move(sym, direction,
                                         baseline_price=ref_price,
                                         wait_sec=30, threshold_pct=0.4)
        # Tier downgrade if not confirmed AND tier was 1
        eff_tier = ev.tier
        if not confirmed and eff_tier == 1:
            eff_tier = 2

        # Use realised move so far
        post_price = _hl_last_price(coin) or ref_price
        move_pct = (post_price - ref_price) / ref_price * 100
        if direction == "down":
            move_pct = -abs(move_pct) if move_pct > 0 else move_pct

        feedback = {
            "pn_id": f"{cycle_id}-{sym}",
            "ts_utc": datetime.now(timezone.utc).isoformat(),
            "event_class": ev.event_class,
            "tier_initial": ev.tier,
            "tier_effective": eff_tier,
            "predicted_dir": direction,
            "baseline_score": ev.baseline_score,
            "confidence": ev.confidence,
            "symbol": sym,
            "ref_price": ref_price,
            "price_after_30s": post_price,
            "realised_pct_30s": round(move_pct, 3),
            "confirmed": confirmed,
            "headline": ev.headline,
            "sources": ev.sources,
            "would_fire": eff_tier == 1,
        }

        if eff_tier == 1 and not dry_run:
            # Fire the 3-stage wave
            try:
                fire_wave(
                    symbol=sym, hl_asset=coin, ref_price=ref_price,
                    move_pct=move_pct, catalyst=catalyst,
                    next_event=None,
                )
                fired += 1
                feedback["fired_at_iso"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                logger.warning(f"[svc] fire_wave({sym}) failed: {e}")
                feedback["fire_error"] = str(e)
        else:
            feedback["fired_at_iso"] = None
            feedback["skipped_reason"] = "tier2" if eff_tier == 2 else "dry_run"

        _feedback_log(feedback)
        logger.info(
            f"[svc] {ev.event_class}/{sym} dir={direction} "
            f"tier={eff_tier} conf={confirmed} "
            f"realised={feedback['realised_pct_30s']:+.2f}% "
            f"fired={feedback['would_fire']}"
        )

    return fired


def run_cycle(*, dry_run: bool = False) -> int:
    """Single scan + process pass. Returns # of events fired."""
    # Pull headlines from each scan query, merge
    all_headlines: list[dict] = []
    seen_titles: set[str] = set()
    for q in SCAN_QUERIES:
        try:
            hls = fetch_top_headlines(q, n=5)
        except Exception as e:
            logger.warning(f"[svc] news fetch failed for '{q}': {e}")
            continue
        for h in hls:
            title = h.title.strip()
            if title in seen_titles:
                continue
            seen_titles.add(title)
            all_headlines.append({
                "title": h.title, "source": h.source, "age": h.when,
                "_age_sec": h.__dict__.get("_age_sec"),
            })
    logger.info(f"[svc] pulled {len(all_headlines)} unique headlines")

    if not all_headlines:
        return 0

    # Detect events
    events = detect_events_in_headlines(all_headlines)
    if not events:
        logger.info("[svc] no events matched")
        return 0

    # Tier assignment (VIX + IST hour aware)
    vix = _vix_proxy()
    ist_hour = _ist_now().hour
    events = [assign_tier(ev, vix_pct=vix, ist_hour=ist_hour) for ev in events]

    # Dedupe by event_class within seen_db (24h window)
    seen = _load_seen()
    now_iso = datetime.now(timezone.utc).isoformat()
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    seen = {k: v for k, v in seen.items() if v > cutoff}

    fresh_events = [ev for ev in events if ev.event_class not in seen]
    for ev in fresh_events:
        seen[ev.event_class] = now_iso
    _save_seen(seen)

    if not fresh_events:
        logger.info(f"[svc] {len(events)} events all seen in last 24h")
        return 0

    fired = 0
    for ev in fresh_events:
        logger.info(
            f"[svc] EVENT {ev.event_class} score={ev.baseline_score:+.2f} "
            f"tier={ev.tier} conf={ev.confidence} "
            f"src(A/B)={ev.source_count_a}/{ev.source_count_b} "
            f"head={(ev.headline or '')[:80]}"
        )
        fired += process_event(ev, dry_run=dry_run)
    return fired


# ── Daemon ──────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cycle-sec", type=int, default=int(os.environ.get("CYCLE_SEC", 60)))
    p.add_argument("--dry-run", action="store_true",
                   help="detect + log + score, but do NOT fire waves")
    p.add_argument("--once", action="store_true",
                   help="run a single cycle and exit")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                          format="%(asctime)s | %(levelname)s | %(message)s")
    logger.info(f"Sentiment-PN service starting · cycle={args.cycle_sec}s · "
                  f"dry_run={args.dry_run}")

    if args.once:
        n = run_cycle(dry_run=args.dry_run)
        logger.info(f"Cycle complete · waves fired: {n}")
        return

    while True:
        try:
            n = run_cycle(dry_run=args.dry_run)
            logger.info(f"Cycle complete · waves fired: {n}")
        except Exception as e:
            logger.exception(f"Cycle failed: {e}")
        time.sleep(args.cycle_sec)


if __name__ == "__main__":
    main()
