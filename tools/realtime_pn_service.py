#!/usr/bin/env python3
"""
tools/realtime_pn_service.py
────────────────────────────────────────────────────────────────────
Unified real-time PN service. Polls every 15 s and dispatches
multiple PN types as conditions match — without blocking on price
confirmation. Each PN is built with answer-style copy, not questions.

PN types fired:
  • SENTIMENT     — keyword event detected in fresh news (tier 1+)
  • VOLUME_SPIKE  — single ticker volume z-score > 3
  • CLUSTER_SHIFT — 3+ clusters tilt same direction in last 30 m
  • BREAKOUT      — ticker closes above 20-bar high or below 20-bar low
  • RECAP         — once-per-day at 02:00 IST (US close summary)

For each fire:
  1. Build compact title + answer-style body (≤ 130 chars total)
  2. Send to Telegram immediately (no 30 s wait)
  3. Async price-confirmation thread runs in background, appends
     follow-up tier upgrade/downgrade to the same JSONL
  4. Per-event dedupe (24h window for sentiment; 1h for others)

Usage:
  python3 tools/realtime_pn_service.py                  # daemon, default 15s
  python3 tools/realtime_pn_service.py --once           # single cycle
  python3 tools/realtime_pn_service.py --types volume,breakout
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analysis.system_v2 import TICKER_CLUSTER
from analysis.top_movers import (
    PRIMARY_CLUSTERS, SECONDARY_CLUSTERS,
    compute_movers, classify_condition, cluster_heatmap, infer_narrative,
)
from analysis.sentiment_engine import (
    detect_events_in_headlines, assign_tier, _vix_proxy,
    confirm_price_move, SentimentEvent,
)
from events.news_search import fetch_top_headlines, reason_for_ticker
from notifiers.compact_intel import IntelInputs, fetch_intel_signals, build_intel_body

logger = logging.getLogger(__name__)
IST = timezone(timedelta(hours=5, minutes=30))


# ── Config ───────────────────────────────────────────────────────

CYCLE_SEC = int(os.environ.get("CYCLE_SEC", 15))
JSONL_PATH = Path(os.environ.get("PN_JSONL", "eval/realtime_pn.jsonl"))
SEEN_PATH = Path(os.environ.get("PN_SEEN_DB", "eval/realtime_seen.json"))

VOL_SPIKE_Z = float(os.environ.get("VOL_SPIKE_Z", 3.0))
CLUSTER_SHIFT_MIN = int(os.environ.get("CLUSTER_SHIFT_MIN", 3))
BREAKOUT_LOOKBACK = 20

# Per-type cooldown (seconds)
COOLDOWN = {
    "sentiment": 24 * 3600,    # 24h dedupe per event class
    "volume_spike": 90 * 60,   # 90m per ticker
    "cluster_shift": 60 * 60,  # 1h
    "breakout":   90 * 60,
    "recap":      18 * 3600,   # max one daily recap per ~18h
}

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


# ── Telegram send (HTML) ─────────────────────────────────────────

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
    ], capture_output=True, text=True, timeout=10)
    return '"ok":true' in r.stdout


def _hl_last_price(coin: str) -> float | None:
    """Quick last-price probe for confirmation threads."""
    payload = (
        '{"type":"candleSnapshot","req":{"coin":"' + coin
        + '","interval":"1m","startTime":' + str(int(time.time()*1000) - 3*60*1000)
        + ',"endTime":' + str(int(time.time()*1000)) + '}}'
    )
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=6,
        )
        arr = json.loads(r.stdout)
        if isinstance(arr, list) and arr:
            return float(arr[-1]["c"])
    except Exception:
        pass
    return None


# ── Seen-tracking helpers ───────────────────────────────────────

def _load_seen() -> dict:
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text())
    except Exception:
        return {}


def _save_seen(d: dict) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(d))


def _allowed(seen: dict, key: str, kind: str) -> bool:
    last = seen.get(key, 0)
    return (time.time() - last) > COOLDOWN.get(kind, 3600)


def _stamp(seen: dict, key: str) -> None:
    seen[key] = time.time()


def _log_jsonl(record: dict) -> None:
    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ── Compact PN composers (answer-style, no questions) ───────────

def _emoji_for_move(m: float) -> str:
    return "🔴" if m < -0.3 else "🟢" if m > 0.3 else "⚪"


def _truncate(s: str, n: int) -> str:
    s = s.strip()
    return s if len(s) <= n else (s[: n - 1].rstrip(" ,.;:") + "…")


def pn_volume_spike(sym: str, move_pct: float, vol_z: float,
                     cluster: str) -> tuple[str, str]:
    emoji = _emoji_for_move(move_pct)
    title = _truncate(f"{emoji} Volume spike · {sym} {move_pct:+.1f}%", 50)

    intel = fetch_intel_signals(sym, f"xyz:{sym}")
    rx = reason_for_ticker(sym, cluster) or {}
    reason = rx.get("reason")
    why_bits = []
    if reason:
        why_bits.append(reason.capitalize())
    why_bits.append(f"vol {vol_z:.1f}× normal")
    if intel.get("near_vwap") == "above":
        why_bits.append("above day avg")
    elif intel.get("near_vwap") == "below":
        why_bits.append("below day avg")
    body = _truncate(". ".join(why_bits) + ".", 80)
    return title, body


def pn_cluster_shift(direction: str, clusters: list[str],
                       avg_moves: dict[str, float]) -> tuple[str, str]:
    emoji = "🔴" if direction == "down" else "🟢"
    side = "selloff" if direction == "down" else "bid"
    title = _truncate(f"{emoji} Broad {side} · {len(clusters)} clusters", 50)
    bits = []
    for c in clusters[:3]:
        m = avg_moves.get(c, 0)
        bits.append(f"{c} {m:+.1f}%")
    body = _truncate(", ".join(bits) + f". Risk-{('off' if direction == 'down' else 'on')} mode.", 80)
    return title, body


def pn_breakout(sym: str, move_pct: float, breakout_dir: str,
                  level: float, cluster: str) -> tuple[str, str]:
    emoji = "🟢" if breakout_dir == "up" else "🔴"
    title = _truncate(f"{emoji} Breakout · {sym} ${level:.2f} {breakout_dir}", 50)
    intel = fetch_intel_signals(sym, f"xyz:{sym}")
    bits = []
    if intel.get("volume_zscore") and intel["volume_zscore"] >= 1.5:
        bits.append("heavy volume confirms")
    if intel.get("atr_ratio") and intel["atr_ratio"] >= 1.5:
        bits.append("expanding range")
    if not bits:
        bits.append("range broken")
    if breakout_dir == "up":
        bits.append("Buy zone")
    else:
        bits.append("Sell zone")
    body = _truncate(", ".join(bits) + ".", 80)
    return title, body


def pn_sentiment(ev: SentimentEvent, sym: str, ref_price: float,
                  realised_pct: float) -> tuple[str, str]:
    emoji = "🔴" if ev.baseline_score < 0 else "🟢"
    catalyst_short = ev.event_class.replace("_", " ").title()
    title = _truncate(f"{emoji} {catalyst_short} · {sym} {realised_pct:+.1f}%", 50)
    bits = []
    if ev.headline:
        h = ev.headline
        # If the headline is a question, drop it and use class label only
        if h.rstrip().endswith("?") or h.lower().startswith("why "):
            pass
        else:
            bits.append(_truncate(h, 50))
    bits.append(f"Conf {int(ev.confidence*100)}%")
    if ev.baseline_score < -0.4:
        bits.append("Sell zone")
    elif ev.baseline_score > 0.4:
        bits.append("Buy zone")
    else:
        bits.append("Wait")
    body = _truncate(". ".join(bits) + ".", 80)
    return title, body


def pn_recap(by_cluster: dict, top_winners: list[dict],
              top_losers: list[dict]) -> tuple[str, str]:
    title = _truncate("📊 US session recap · daily wrap", 50)
    bits = []
    if top_winners:
        w = top_winners[0]
        bits.append(f"Top: {w['symbol']} {w['move_24h_pct']:+.1f}%")
    if top_losers:
        l = top_losers[0]
        bits.append(f"Worst: {l['symbol']} {l['move_24h_pct']:+.1f}%")
    if not bits:
        bits.append("Tape mixed")
    bits.append("Setup tomorrow")
    body = _truncate(", ".join(bits) + ".", 80)
    return title, body


# ── Detectors ────────────────────────────────────────────────────

def detect_volume_spikes(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        sym = r["symbol"]
        intel = fetch_intel_signals(sym, f"xyz:{sym}")
        z = intel.get("volume_zscore")
        if z is not None and z >= VOL_SPIKE_Z:
            out.append({
                "symbol": sym, "vol_z": z,
                "move_pct": r["move_pct"],
                "cluster": r.get("cluster", "other"),
            })
    return out


def detect_cluster_shift(rows: list[dict]) -> dict | None:
    hm = cluster_heatmap(rows)
    tilts = {c: v for c, v in hm.items() if v["n"] >= 2}
    n_up = sum(1 for v in tilts.values() if v["tilt"] == "up")
    n_dn = sum(1 for v in tilts.values() if v["tilt"] == "dn")
    avg_moves = {c: v["avg_move"] for c, v in tilts.items()}
    if n_dn >= CLUSTER_SHIFT_MIN and n_up <= 1:
        clusters = [c for c, v in tilts.items() if v["tilt"] == "dn"]
        return {"direction": "down", "clusters": clusters, "avg_moves": avg_moves}
    if n_up >= CLUSTER_SHIFT_MIN and n_dn <= 1:
        clusters = [c for c, v in tilts.items() if v["tilt"] == "up"]
        return {"direction": "up", "clusters": clusters, "avg_moves": avg_moves}
    return None


def detect_breakouts(rows: list[dict]) -> list[dict]:
    """For each row, fetch 25 bars and check Donchian-20 break."""
    out = []
    for r in rows:
        sym = r["symbol"]
        coin = f"xyz:{sym}"
        # Fetch ~25 bars
        payload = (
            '{"type":"candleSnapshot","req":{"coin":"' + coin
            + '","interval":"15m","startTime":'
            + str(int(time.time()*1000) - 30 * 15 * 60 * 1000)
            + ',"endTime":' + str(int(time.time()*1000)) + '}}'
        )
        try:
            res = subprocess.run(
                ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
                 "-H", "Content-Type: application/json", "-d", payload],
                capture_output=True, text=True, timeout=8,
            )
            arr = json.loads(res.stdout)
            if not isinstance(arr, list) or len(arr) < 22:
                continue
        except Exception:
            continue
        prior_high = max(float(c["h"]) for c in arr[-21:-1])
        prior_low = min(float(c["l"]) for c in arr[-21:-1])
        last_close = float(arr[-1]["c"])
        if last_close > prior_high:
            out.append({"symbol": sym, "breakout_dir": "up",
                        "level": prior_high, "move_pct": r["move_pct"],
                        "cluster": r.get("cluster", "other")})
        elif last_close < prior_low:
            out.append({"symbol": sym, "breakout_dir": "down",
                        "level": prior_low, "move_pct": r["move_pct"],
                        "cluster": r.get("cluster", "other")})
    return out


# ── Orchestrator ────────────────────────────────────────────────

def fire_pn(kind: str, title: str, body: str, payload: dict,
              dry_run: bool = False) -> bool:
    pn_id = str(uuid.uuid4())[:8]
    msg = f"<b>{title}</b>\n{body}"
    record = {
        "pn_id": pn_id, "kind": kind, "title": title, "body": body,
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "ts_ist": datetime.now(IST).isoformat(timespec="seconds"),
        "payload": payload,
    }
    sent = False
    if not dry_run:
        sent = _send(msg)
    record["sent"] = sent
    _log_jsonl(record)
    if sent or dry_run:
        logger.info(f"[{kind}] {title}  ·  {len(title)+len(body)+1} chars")
    return sent


def cycle(args, seen: dict) -> None:
    enabled = set(args.types.split(",")) if args.types else {
        "sentiment", "volume_spike", "cluster_shift", "breakout", "recap"
    }
    primary_types = {"sentiment", "volume_spike", "cluster_shift", "breakout"}

    rows = compute_movers(window_min=30, universe_mode="all", focus="all")
    if rows:
        # Filter to PRIMARY tickers (stocks + indices) for non-recap PN types
        primary_rows = [r for r in rows if r["cluster"] in PRIMARY_CLUSTERS]
        secondary_rows = [r for r in rows if r["cluster"] in SECONDARY_CLUSTERS]
    else:
        primary_rows = secondary_rows = []

    # Volume spikes — primary first, fall back to secondary if none
    if "volume_spike" in enabled and primary_rows:
        spikes = detect_volume_spikes(primary_rows)
        if not spikes and secondary_rows:
            spikes = detect_volume_spikes(secondary_rows)
        for s in spikes[:2]:
            key = f"volspike:{s['symbol']}"
            if not _allowed(seen, key, "volume_spike"):
                continue
            title, body = pn_volume_spike(s["symbol"], s["move_pct"],
                                            s["vol_z"], s["cluster"])
            fire_pn("volume_spike", title, body, s, dry_run=args.dry_run)
            _stamp(seen, key)

    # Cluster shift
    if "cluster_shift" in enabled and rows:
        shift = detect_cluster_shift(rows)
        if shift:
            key = f"clustershift:{shift['direction']}"
            if _allowed(seen, key, "cluster_shift"):
                title, body = pn_cluster_shift(shift["direction"],
                                                shift["clusters"],
                                                shift["avg_moves"])
                fire_pn("cluster_shift", title, body, shift, dry_run=args.dry_run)
                _stamp(seen, key)

    # Breakout
    if "breakout" in enabled and primary_rows:
        bos = detect_breakouts(primary_rows[:8])
        for b in bos[:2]:
            key = f"breakout:{b['symbol']}"
            if not _allowed(seen, key, "breakout"):
                continue
            title, body = pn_breakout(b["symbol"], b["move_pct"],
                                        b["breakout_dir"], b["level"],
                                        b["cluster"])
            fire_pn("breakout", title, body, b, dry_run=args.dry_run)
            _stamp(seen, key)

    # Mutually-exclusive event pairs: when both detected in one cycle,
    # the higher-confidence one wins. Prevents "BTC crash" + "BTC rally"
    # from firing together.
    EVENT_PAIRS = {
        "btc_crash": "btc_rally",
        "btc_rally": "btc_crash",
        "fed_hawkish": "fed_dovish",
        "fed_dovish": "fed_hawkish",
        "cpi_hot": "cpi_cool",
        "cpi_cool": "cpi_hot",
        "earnings_beat": "earnings_miss",
        "earnings_miss": "earnings_beat",
        "guidance_raise": "guidance_cut",
        "guidance_cut": "guidance_raise",
    }
    MAX_SENTIMENT_PER_CYCLE = 1

    # Sentiment events
    if "sentiment" in enabled:
        all_h: list[dict] = []
        seen_titles: set[str] = set()
        for q in SCAN_QUERIES:
            try:
                hls = fetch_top_headlines(q, n=4)
            except Exception:
                continue
            for h in hls:
                if h.title in seen_titles: continue
                seen_titles.add(h.title)
                all_h.append({"title": h.title, "source": h.source,
                              "age": h.when,
                              "_age_sec": h.__dict__.get("_age_sec")})
        events = detect_events_in_headlines(all_h)
        vix = _vix_proxy()
        ist_h = datetime.now(IST).hour
        for ev in events:
            assign_tier(ev, vix_pct=vix, ist_hour=ist_h)

        # Mutual exclusion: when both halves of a pair appear, keep
        # only the higher-confidence one.
        ev_by_class = {ev.event_class: ev for ev in events}
        kept: dict[str, SentimentEvent] = {}
        for ev in events:
            opp = EVENT_PAIRS.get(ev.event_class)
            if opp and opp in ev_by_class:
                rival = ev_by_class[opp]
                if ev.confidence < rival.confidence:
                    continue
                if ev.confidence == rival.confidence and ev.event_class > opp:
                    continue
            kept[ev.event_class] = ev
        # Sort by confidence desc, take top N per cycle
        ranked = sorted(kept.values(), key=lambda x: -x.confidence)

        fired_count = 0
        for ev in ranked:
            if fired_count >= MAX_SENTIMENT_PER_CYCLE:
                break
            ek = f"sentiment:{ev.event_class}"
            if not _allowed(seen, ek, "sentiment"):
                continue
            if ev.tier != 1:
                logger.info(f"[sentiment] tier2 skip: {ev.event_class}")
                continue
            # Pick first impacted ticker for the PN
            impacted_clusters = [c for c, *_ in ev.impacted if c != "idio"]
            target = None
            for r in primary_rows:
                if r["cluster"] in impacted_clusters:
                    target = r; break
            if not target and rows:
                target = rows[0]
            if not target:
                continue
            sym = target["symbol"]
            ref_price = _hl_last_price(f"xyz:{sym}") or target["price"]
            title, body = pn_sentiment(ev, sym, ref_price, target["move_pct"])
            fire_pn("sentiment", title, body, {
                "event_class": ev.event_class,
                "score": ev.baseline_score,
                "confidence": ev.confidence,
                "sources": ev.sources,
                "headline": ev.headline,
            }, dry_run=args.dry_run)
            _stamp(seen, ek)
            fired_count += 1
            # Async price-confirmation thread (logs upgrade/downgrade)
            threading.Thread(target=_async_confirm,
                              args=(ev, sym, ref_price), daemon=True).start()

    # Daily recap (around 02:00 IST = US close)
    if "recap" in enabled and rows:
        ist_h = datetime.now(IST).hour
        if 1 <= ist_h <= 3:
            key = "recap:" + datetime.now(IST).date().isoformat()
            if _allowed(seen, key, "recap"):
                rs = sorted(rows, key=lambda r: r.get("move_24h_pct", 0))
                losers = rs[:3]
                winners = rs[-3:][::-1]
                hm = cluster_heatmap(rows)
                title, body = pn_recap(hm, winners, losers)
                fire_pn("recap", title, body, {
                    "winners": [w["symbol"] for w in winners],
                    "losers": [l["symbol"] for l in losers],
                }, dry_run=args.dry_run)
                _stamp(seen, key)

    _save_seen(seen)


def _async_confirm(ev: SentimentEvent, sym: str, ref_price: float) -> None:
    """Background tier-confirmation logger."""
    direction = "down" if ev.baseline_score < 0 else "up"
    confirmed = confirm_price_move(sym, direction,
                                     baseline_price=ref_price,
                                     wait_sec=60, threshold_pct=0.4)
    _log_jsonl({
        "kind": "tier_confirm",
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "event_class": ev.event_class,
        "symbol": sym,
        "predicted_dir": direction,
        "ref_price": ref_price,
        "confirmed": confirmed,
    })


# ── Driver ──────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cycle-sec", type=int, default=CYCLE_SEC)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--types", default=None,
                   help="comma-separated subset of {sentiment,volume_spike,cluster_shift,breakout,recap}")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                          format="%(asctime)s | %(levelname)s | %(message)s")
    logger.info(f"Realtime PN service · cycle={args.cycle_sec}s "
                  f"types={args.types or 'ALL'} dry={args.dry_run}")

    seen = _load_seen()
    if args.once:
        cycle(args, seen)
        return
    while True:
        try:
            cycle(args, seen)
        except Exception as e:
            logger.exception(f"cycle failed: {e}")
        time.sleep(args.cycle_sec)


if __name__ == "__main__":
    main()
