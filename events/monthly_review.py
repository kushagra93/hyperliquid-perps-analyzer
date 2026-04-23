# events/monthly_review.py
# ─────────────────────────────────────────────────────────────────
# Monthly earnings review + rule-based forward lean for each tracked
# ticker.
#
# Past-earnings review (last N days):
#   - Finnhub /stock/earnings for last quarters reported in-window
#   - HL daily candles around each earnings date → actual post-
#     earnings move (T-1 close → T+1 close)
#   - Surprise % vs consensus
#
# Forward forecast (upcoming N days):
#   - Four deterministic signals per ticker, each -1 / 0 / +1:
#       1. Recent beat/miss streak (last 4 quarters)
#       2. Analyst recommendation tilt (Finnhub /stock/recommendation)
#       3. Consensus price-target vs spot (Finnhub /stock/price-target)
#       4. Ticker-specific news keyword sentiment (Finnhub /company-news)
#     Summed to a -4..+4 score, bucketed into lean:
#       ≥ +3 STRONG BULLISH
#       +1 / +2 MODERATE BULLISH
#       0 NEUTRAL
#       -1 / -2 MODERATE BEARISH
#       ≤ -3 STRONG BEARISH
#   - No LLM inference; every signal cites numeric source.
#
# All Finnhub endpoints are free-tier. Cached through events/fetcher's
# 24h JSON cache where possible.
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import date, datetime, timedelta
from typing import Any

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from events.fetcher import (
    _BASE, _cache_get, _cache_put, _finnhub_get,
    get_earnings_calendar, get_earnings_history,
)

logger = logging.getLogger(__name__)

BULL_WORDS = {
    "upgrade", "beat", "beats", "raised", "raises", "partnership", "acquire",
    "acquires", "acquisition", "expand", "expansion", "growth", "record",
    "boost", "boosts", "surge", "surges", "strong", "bullish", "breakout",
    "breakthrough", "milestone", "approval", "approved", "launch", "launches",
    "innovation", "rally",
}
BEAR_WORDS = {
    "downgrade", "miss", "misses", "lowered", "lowers", "lawsuit", "sue",
    "probe", "investigation", "drop", "slump", "slumps", "warning", "warn",
    "cut", "cuts", "weak", "fall", "falls", "loss", "losses", "bearish",
    "layoff", "layoffs", "delay", "delays", "recall", "downturn", "fraud",
}


# ── HL daily candles around a date ────────────────────────────────

def _hl_daily(coin: str, lookback_days: int = 400) -> list[tuple]:
    now = int(time.time() * 1000)
    payload = {"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "1d",
        "startTime": now - lookback_days * 24 * 3600 * 1000,
        "endTime": now,
    }}
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=15,
        )
        arr = json.loads(r.stdout)
        return [(int(c["t"]), float(c["o"]), float(c["h"]),
                 float(c["l"]), float(c["c"])) for c in arr]
    except Exception:
        return []


def _candle_before(candles: list[tuple], ts_ms: int) -> tuple | None:
    best = None
    for c in candles:
        if c[0] <= ts_ms:
            best = c
        else:
            break
    return best


def _candle_after(candles: list[tuple], ts_ms: int) -> tuple | None:
    for c in candles:
        if c[0] >= ts_ms:
            return c
    return None


def _earnings_move_pct(coin: str, earnings_iso: str) -> float | None:
    try:
        d = datetime.fromisoformat(earnings_iso)
    except Exception:
        return None
    ts = int(d.timestamp() * 1000)
    cs = _hl_daily(coin, 400)
    if not cs:
        return None
    prev = _candle_before(cs, ts - 24 * 3600 * 1000)
    nxt = _candle_after(cs, ts + 24 * 3600 * 1000)
    if not prev or not nxt or prev[4] <= 0:
        return None
    return (nxt[4] - prev[4]) / prev[4] * 100


# ── Finnhub helpers for forecast ──────────────────────────────────

def _recommendations(sym: str) -> list[dict]:
    """
    /stock/recommendation → list of monthly snapshots with
    {period, buy, hold, sell, strongBuy, strongSell}.
    """
    key = f"rec_{sym.upper()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    data = _finnhub_get("/stock/recommendation", {"symbol": sym.upper()})
    out = data if isinstance(data, list) else []
    _cache_put(key, out)
    return out


def _price_target(sym: str) -> dict:
    """/stock/price-target → {targetHigh, targetLow, targetMean, ...}
    Note: premium endpoint on Finnhub free tier (HTTP 403). We cache
    the empty response so we don't retry every call."""
    key = f"pt_{sym.upper()}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    # Silently tolerate 403 on free tier
    import logging as _logging
    prev = _logging.getLogger("events.fetcher").level
    _logging.getLogger("events.fetcher").setLevel(_logging.ERROR)
    try:
        data = _finnhub_get("/stock/price-target", {"symbol": sym.upper()})
    finally:
        _logging.getLogger("events.fetcher").setLevel(prev)
    out = data if isinstance(data, dict) else {}
    _cache_put(key, out)
    return out


def _company_news(sym: str, days: int = 14) -> list[dict]:
    """/company-news → list of articles within the window."""
    key = f"cnews_{sym.upper()}_{days}"
    cached = _cache_get(key)
    if cached is not None:
        return cached
    today = date.today()
    data = _finnhub_get("/company-news", {
        "symbol": sym.upper(),
        "from": (today - timedelta(days=days)).isoformat(),
        "to": today.isoformat(),
    })
    out = data if isinstance(data, list) else []
    _cache_put(key, out)
    return out


# ── Signal computation ────────────────────────────────────────────

def _beat_miss_streak_signal(hist: list[dict]) -> tuple[int, str]:
    """Last up-to-4 quarters: count beats vs misses via surprise %."""
    if not hist:
        return 0, "no earnings history"
    beats = sum(1 for r in hist if (r.get("surprise_pct") or 0) > 0.5)
    misses = sum(1 for r in hist if (r.get("surprise_pct") or 0) < -0.5)
    net = beats - misses
    if net >= 2: return +1, f"{beats}/{len(hist)} beats, {misses} misses"
    if net <= -2: return -1, f"{misses}/{len(hist)} misses, {beats} beats"
    return 0, f"{beats} beats / {misses} misses (mixed)"


def _analyst_rec_signal(recs: list[dict]) -> tuple[int, str]:
    """Most recent analyst snapshot: net (strongBuy+buy) vs (strongSell+sell)."""
    if not recs:
        return 0, "no analyst coverage"
    latest = sorted(recs, key=lambda r: r.get("period") or "", reverse=True)[0]
    pos = (latest.get("strongBuy") or 0) + (latest.get("buy") or 0)
    neg = (latest.get("strongSell") or 0) + (latest.get("sell") or 0)
    hold = latest.get("hold") or 0
    total = pos + neg + hold
    if total == 0:
        return 0, "n/a"
    pos_pct = pos / total
    if pos_pct >= 0.65 and pos >= neg * 2:
        return +1, f"{pos}B/{hold}H/{neg}S ({pos_pct*100:.0f}% buy)"
    if (neg / total) >= 0.35:
        return -1, f"{pos}B/{hold}H/{neg}S (bearish tilt)"
    return 0, f"{pos}B/{hold}H/{neg}S (mixed)"


def _price_target_signal(pt: dict, current: float) -> tuple[int, str]:
    mean = pt.get("targetMean")
    if not mean or current <= 0:
        return 0, "no target"
    diff = (mean - current) / current * 100
    if diff >= 5:
        return +1, f"mean ${mean:.2f} (+{diff:.1f}% vs spot ${current:.2f})"
    if diff <= -5:
        return -1, f"mean ${mean:.2f} ({diff:.1f}% vs spot ${current:.2f})"
    return 0, f"mean ${mean:.2f} (~spot ${current:.2f})"


def _news_sentiment_signal(news: list[dict]) -> tuple[int, str]:
    if not news:
        return 0, "no recent news"
    bull = bear = 0
    # Score titles + first 180 chars of headline/snippet
    for a in news[:40]:
        txt = f"{a.get('headline','')} {a.get('summary','')}"[:400].lower()
        for w in BULL_WORDS:
            if w in txt: bull += 1; break
        for w in BEAR_WORDS:
            if w in txt: bear += 1; break
    if bull - bear >= 4:
        return +1, f"{bull} bullish vs {bear} bearish keywords"
    if bear - bull >= 4:
        return -1, f"{bear} bearish vs {bull} bullish keywords"
    return 0, f"{bull} bullish / {bear} bearish (mixed)"


LEAN_LABELS = {
    4: "🟢🟢 STRONG BULLISH", 3: "🟢🟢 STRONG BULLISH",
    2: "🟢 MODERATE BULLISH", 1: "🟢 MODERATE BULLISH",
    0: "⚪ NEUTRAL",
    -1: "🔴 MODERATE BEARISH", -2: "🔴 MODERATE BEARISH",
    -3: "🔴🔴 STRONG BEARISH", -4: "🔴🔴 STRONG BEARISH",
}


def forecast_for_ticker(sym: str, coin: str, current_price: float,
                         next_earnings_date: str | None) -> dict:
    hist = get_earnings_history(sym, limit=4)
    recs = _recommendations(sym)
    pt = _price_target(sym)
    news = _company_news(sym, days=14)

    s1, d1 = _beat_miss_streak_signal(hist)
    s2, d2 = _analyst_rec_signal(recs)
    s3, d3 = _price_target_signal(pt, current_price)
    s4, d4 = _news_sentiment_signal(news)
    total = s1 + s2 + s3 + s4

    return {
        "symbol": sym,
        "current_price": current_price,
        "next_earnings_date": next_earnings_date,
        "score": total,
        "lean": LEAN_LABELS.get(total, "⚪ NEUTRAL"),
        "signals": {
            "beat_miss_streak":   {"score": s1, "detail": d1},
            "analyst_recs":       {"score": s2, "detail": d2},
            "price_target":       {"score": s3, "detail": d3},
            "news_sentiment":     {"score": s4, "detail": d4},
        },
        "top_headlines": [a.get("headline") for a in news[:5] if a.get("headline")],
    }


# ── Public reports ────────────────────────────────────────────────

def review_past(tickers: list[str], days: int = 30) -> list[dict]:
    """For each ticker, collect earnings reported in the last `days` days."""
    today = date.today()
    cutoff_iso = (today - timedelta(days=days)).isoformat()
    rows: list[dict] = []
    for sym in tickers:
        hist = get_earnings_history(sym, limit=4)
        for h in hist:
            period = h.get("period") or ""
            if period < cutoff_iso:
                continue
            coin = f"xyz:{sym}"
            move = _earnings_move_pct(coin, period)
            surprise = h.get("surprise_pct")
            eps_actual = h.get("eps_actual")
            eps_est = h.get("eps_estimate")
            verdict = "beat" if (surprise or 0) > 0.5 else \
                      "miss" if (surprise or 0) < -0.5 else "in-line"
            rows.append({
                "symbol": sym, "period": period,
                "eps_actual": eps_actual, "eps_estimate": eps_est,
                "surprise_pct": surprise, "verdict": verdict,
                "post_earnings_move_pct": round(move, 2) if move is not None else None,
            })
    rows.sort(key=lambda r: r["period"], reverse=True)
    return rows


def forecast_upcoming(tickers: list[str], days: int = 30) -> list[dict]:
    """For each ticker with earnings in next `days` days, return a forecast row."""
    cal = get_earnings_calendar(days)
    by_sym = {r["symbol"]: r for r in cal if r["symbol"] in set(tickers)}
    out: list[dict] = []
    today = date.today()

    for sym, er in by_sym.items():
        coin = f"xyz:{sym}"
        now = int(time.time() * 1000)
        # Get current price via 15m candle
        p = {"type": "candleSnapshot", "req": {
            "coin": coin, "interval": "15m",
            "startTime": now - 6 * 3600 * 1000, "endTime": now,
        }}
        try:
            r = subprocess.run(
                ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
                 "-H", "Content-Type: application/json",
                 "-d", json.dumps(p)],
                capture_output=True, text=True, timeout=10,
            )
            arr = json.loads(r.stdout)
            price = float(arr[-1]["c"]) if arr else 0.0
        except Exception:
            price = 0.0
        if not price:
            continue

        fcst = forecast_for_ticker(sym, coin, price, er["date"])
        try:
            fcst["days_to_earnings"] = (
                datetime.fromisoformat(er["date"]).date() - today
            ).days
        except Exception:
            fcst["days_to_earnings"] = None
        fcst["earnings_hour"] = er.get("hour")
        fcst["eps_estimate"] = er.get("eps_estimate")
        out.append(fcst)

    out.sort(key=lambda r: r.get("days_to_earnings") or 99)
    return out


def monthly_report(tickers: list[str], days: int = 30) -> dict:
    return {
        "as_of": datetime.utcnow().isoformat() + "Z",
        "tickers": tickers,
        "past_earnings": review_past(tickers, days),
        "upcoming_forecasts": forecast_upcoming(tickers, days),
    }
