# analysis/sentiment_engine.py
# ─────────────────────────────────────────────────────────────────
# Event detection + sentiment scoring + tier assignment.
#
# Layer 1: rule-based keyword pattern → event class + baseline score
# Layer 2: multi-source confirmation (≥2 credible outlets in 60 min)
# Layer 3: price confirmation (T+30s HL poll — did the move happen?)
#
# Each detected event yields a SentimentEvent with:
#   • event_class    (trump_tariff, fed_hawkish, china_trade, ...)
#   • impacted       (list of (ticker, predicted_dir, magnitude_baseline))
#   • baseline_score (-1.0..+1.0 sentiment)
#   • tier           (1 = high-confidence push; 2 = log only)
#   • sources        (list of source attributions)
#   • headline       (best representative)
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


# ── Event taxonomy ──────────────────────────────────────────────
# Pattern → (event_class, baseline_score, impacted_clusters_with_dir)
EVENT_PATTERNS = [
    # Tariffs / trade
    (r"\btrump\b.{0,30}\btariff", "trump_tariff", -0.80,
       [("semi", "down", 1.5), ("china", "down", 2.0),
        ("mega-tech", "down", 1.0), ("commodity", "up", 0.6)]),
    (r"\btariff\b.{0,30}(china|imposed|extended)", "trade_tariff", -0.65,
       [("semi", "down", 1.0), ("china", "down", 1.5)]),
    (r"\btrade war\b", "trade_war", -0.70,
       [("semi", "down", 1.2), ("china", "down", 1.8), ("mega-tech", "down", 1.0)]),

    # Fed / rates
    (r"\bfomc\b|\bfed\b.{0,30}(hike|hiking|hawkish)", "fed_hawkish", -0.55,
       [("mega-tech", "down", 0.8), ("high-beta", "down", 1.2),
        ("crypto-proxy", "down", 1.5), ("commodity", "down", 0.5)]),
    (r"\bfed\b.{0,30}(cut|cutting|dovish|pause)", "fed_dovish", +0.55,
       [("mega-tech", "up", 0.6), ("high-beta", "up", 1.0),
        ("crypto-proxy", "up", 1.2), ("commodity", "up", 0.4)]),
    (r"\brate (hike|cut)", "rate_decision", 0.0,  # signed downstream
       [("mega-tech", "down", 0.5)]),

    # Inflation / data
    (r"\bcpi\b.{0,30}(hot|above|surge|jump|higher)", "cpi_hot", -0.50,
       [("mega-tech", "down", 0.8), ("commodity", "up", 0.6)]),
    (r"\bcpi\b.{0,30}(cool|below|dropped|easing)", "cpi_cool", +0.45,
       [("mega-tech", "up", 0.6)]),
    (r"\bnfp\b.{0,30}(strong|beat|above)", "nfp_strong", -0.30,
       [("mega-tech", "down", 0.4)]),
    (r"\bnfp\b.{0,30}(weak|miss|below)", "nfp_weak", +0.30,
       [("mega-tech", "up", 0.4)]),

    # Geopolitical
    (r"\b(iran|middle east|israel|gaza)\b.{0,40}(strike|war|attack|escalat)",
       "middle_east_war", -0.60,
       [("commodity", "up", 1.2), ("mega-tech", "down", 0.4)]),
    (r"\bopec\b.{0,30}(cut|reduce|extend)", "opec_supply_cut", +0.50,
       [("commodity", "up", 1.0)]),
    (r"\b(russia|ukraine|putin)\b.{0,40}(strike|war|escalat)", "ukraine_war", -0.55,
       [("commodity", "up", 0.8), ("mega-tech", "down", 0.4)]),

    # Earnings
    (r"\b(beats|tops|beat).{0,15}(estimates|expectations)", "earnings_beat", +0.50,
       [("idio", "up", 1.5)]),
    (r"\b(misses|missed|below).{0,15}(estimates|expectations)", "earnings_miss", -0.50,
       [("idio", "down", 1.5)]),
    (r"\bguidance\b.{0,20}(raised|lifts|hike|above)", "guidance_raise", +0.55,
       [("idio", "up", 2.0)]),
    (r"\bguidance\b.{0,20}(cut|lowered|below)", "guidance_cut", -0.55,
       [("idio", "down", 2.0)]),

    # Crypto
    (r"\bbitcoin\b.{0,30}(crash|plunge|tumble|dump)", "btc_crash", -0.60,
       [("crypto-proxy", "down", 2.0)]),
    (r"\bbitcoin\b.{0,30}(surge|rally|breakout|jump)", "btc_rally", +0.55,
       [("crypto-proxy", "up", 2.0)]),
    (r"\bsec\b.{0,30}(approves|approved|greenlight|etf)", "sec_approve", +0.50,
       [("crypto-proxy", "up", 1.5)]),
    (r"\bsec\b.{0,30}(charges|sue|lawsuit|investigat)", "sec_action", -0.55,
       [("crypto-proxy", "down", 1.5)]),

    # AI / chips
    (r"\bnvidia\b.{0,30}(beats|surge|record)", "nvidia_strong", +0.50,
       [("semi", "up", 1.0)]),
    (r"\bchip\b.{0,30}(ban|export.{0,10}control|restrict)", "chip_export_curb", -0.65,
       [("semi", "down", 1.5), ("china", "down", 1.0)]),

    # M&A
    (r"\b(acquir|takeover|merge)", "m_and_a", +0.40,
       [("idio", "up", 1.5)]),
]


# Source credibility tiers (used for multi-source confirmation)
TIER_A_SOURCES = {
    "reuters", "bloomberg", "wsj", "wall street journal",
    "ft.com", "financial times", "cnbc", "barrons", "barron's",
    "marketwatch", "the economic times", "yahoo finance",
    "associated press", "ap news",
}
TIER_B_SOURCES = {
    "investing.com", "seeking alpha", "fxstreet", "the motley fool",
    "fool.com", "benzinga", "moneycontrol",
}


# ── Public types ────────────────────────────────────────────────

@dataclass
class SentimentEvent:
    event_class: str
    baseline_score: float
    impacted: list[tuple[str, str, float]]   # (cluster_or_idio, dir, magnitude)
    tier: int = 2                            # 1 = push, 2 = log
    sources: list[str] = field(default_factory=list)
    headline: str = ""
    source_count_a: int = 0   # # of TIER_A sources
    source_count_b: int = 0
    confidence: float = 0.0   # 0-1
    detected_at: float = field(default_factory=time.time)


# ── Event detection (Layer 1) ───────────────────────────────────

def detect_events_in_headlines(headlines: list[dict]) -> list[SentimentEvent]:
    """
    Scan recent headlines for known event patterns. Each headline is
    {title, source, age, ...}. Returns one SentimentEvent per matched
    event class with sources merged.
    """
    by_class: dict[str, SentimentEvent] = {}
    for h in headlines:
        title = (h.get("title") or "").lower()
        src = (h.get("source") or "").lower()
        if not title:
            continue
        for rx, evt_class, score, impacted in EVENT_PATTERNS:
            if re.search(rx, title, re.I):
                ev = by_class.get(evt_class)
                if ev is None:
                    ev = SentimentEvent(
                        event_class=evt_class,
                        baseline_score=score,
                        impacted=impacted,
                        headline=h.get("title", ""),
                        sources=[h.get("source", "")] if h.get("source") else [],
                    )
                    by_class[evt_class] = ev
                else:
                    if h.get("source"):
                        ev.sources.append(h["source"])
                # Source-credibility counting
                if any(t in src for t in TIER_A_SOURCES):
                    ev.source_count_a += 1
                elif any(t in src for t in TIER_B_SOURCES):
                    ev.source_count_b += 1
                # Replace headline if this one has more specific facts
                if re.search(r"\d", h.get("title", "")) and not re.search(r"\d", ev.headline):
                    ev.headline = h.get("title", "")
                    ev.sources = [h.get("source", "")] if h.get("source") else []
    return list(by_class.values())


# ── Tier assignment (Layer 2 + 3) ──────────────────────────────

def _vix_proxy() -> float | None:
    """
    Use cross-sectional dispersion of universe 1-day moves as a
    crude VIX proxy. Higher = more volatile regime.
    Returns percentile (0..1) where 1.0 = top decile vol.
    """
    try:
        from analysis.top_movers import compute_movers
        rows = compute_movers(window_min=60 * 24, universe_mode="all")
        if not rows:
            return None
        moves = [abs(r.get("move_24h_pct", 0)) for r in rows]
        if not moves:
            return None
        avg = sum(moves) / len(moves)
        # Map to a percentile: avg 1% → low; avg 3%+ → high
        if avg <= 1.0: return 0.2
        if avg <= 1.5: return 0.4
        if avg <= 2.0: return 0.6
        if avg <= 3.0: return 0.8
        return 1.0
    except Exception:
        return None


def assign_tier(ev: SentimentEvent, *,
                  vix_pct: float | None = None,
                  ist_hour: int | None = None) -> SentimentEvent:
    """
    Multi-factor tier assignment:
      • TIER 1 push if (≥1 TIER_A source AND ≥2 total sources)
                    OR (≥3 TIER_B sources)
      • Volatility regime adjustment (high VIX upgrades, low downgrades)
      • Off-hours: downgrade unless tier_A>=2
    """
    a, b = ev.source_count_a, ev.source_count_b
    total = a + b

    if (a >= 1 and total >= 2) or b >= 3:
        ev.tier = 1
    else:
        ev.tier = 2

    # Volatility regime
    if vix_pct is not None:
        if vix_pct >= 0.8:
            ev.tier = 1   # high vol = signals amplified
        elif vix_pct <= 0.2 and ev.tier == 1 and a < 2:
            ev.tier = 2   # very low vol = downgrade unless multiple TIER_A

    # Off-hours: 2-7 IST, no one to trade — downgrade
    if ist_hour is not None and 2 <= ist_hour < 7:
        if a < 2:
            ev.tier = 2

    # Confidence: 0..1 from sources + magnitude
    src_score = min(a * 0.4 + b * 0.15, 0.85)
    mag_score = min(abs(ev.baseline_score), 1.0) * 0.15
    ev.confidence = round(src_score + mag_score, 2)

    return ev


# ── Layer 3: live price confirmation ────────────────────────────

def _hl_last_price(coin: str) -> float | None:
    import json as _json
    payload = (
        '{"type":"candleSnapshot","req":{"coin":"' + coin
        + '","interval":"1m","startTime":' + str(int(time.time()*1000) - 5*60*1000)
        + ',"endTime":' + str(int(time.time()*1000)) + '}}'
    )
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json", "-d", payload],
            capture_output=True, text=True, timeout=8,
        )
        arr = _json.loads(r.stdout)
        if isinstance(arr, list) and arr:
            return float(arr[-1]["c"])
    except Exception:
        pass
    return None


def confirm_price_move(symbol: str, predicted_dir: str,
                        baseline_price: float | None = None,
                        wait_sec: int = 30,
                        threshold_pct: float = 0.4) -> bool:
    """
    Poll HL for `wait_sec` and check if a move ≥ threshold_pct in
    the predicted direction occurred. Returns True for confirmation.
    """
    coin = f"xyz:{symbol}"
    p0 = baseline_price or _hl_last_price(coin)
    if not p0 or p0 <= 0:
        return False
    deadline = time.time() + wait_sec
    while time.time() < deadline:
        time.sleep(5)
        p1 = _hl_last_price(coin)
        if not p1:
            continue
        change_pct = (p1 - p0) / p0 * 100
        if predicted_dir == "up" and change_pct >= threshold_pct:
            return True
        if predicted_dir == "down" and change_pct <= -threshold_pct:
            return True
    return False
