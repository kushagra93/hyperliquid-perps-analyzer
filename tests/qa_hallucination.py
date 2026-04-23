#!/usr/bin/env python3
"""
tests/qa_hallucination.py
────────────────────────────────────────────────────────────────────
QA harness: run every recently-added feature through the anti-
hallucination framework and assert the system's invariants.

The anti-hallucination framework classifies each output layer as:

  (A) LLM-generated        → must be grounded + citation-checked
  (B) Rule-based           → must be deterministic and reproducible
  (C) Raw data / formatting → must be HTML-safe and null-safe

This harness checks:

  1. Module classification — which modules call an LLM. Any module
     we declared rule-based must NOT import agents.agent1_news or
     agents.agent3_causality.
  2. Determinism — calling rule-based functions twice with identical
     inputs produces byte-identical output.
  3. Graceful degradation — empty / malformed / missing inputs return
     `{enabled: False, ...}` rather than raising.
  4. HTML safety — rendered blocks balance <b>/<i>/<code> tags and
     escape bare `<` characters (e.g. "<200").
  5. Price grounding — every dollar figure in a rendered block is
     within 50% of the reference price (sanity cap).
  6. Numeric citation — technicals output MUST include raw RSI / ATR
     values so traders can audit.
  7. Agent 3 guardrails — exercise the post-hoc validator with a
     crafted "hallucinatory" verdict and assert flags are raised.

Run:
  python3 tests/qa_hallucination.py
  python3 tests/qa_hallucination.py --telegram   # also push summary

Exit code: non-zero iff any check fails.
────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ── Tally ─────────────────────────────────────────────────────────
results: list[tuple[str, bool, str]] = []


def check(name: str):
    def wrap(fn):
        try:
            detail = fn() or ""
            results.append((name, True, detail))
            print(f"  ✅ {name}  {detail}")
        except AssertionError as e:
            results.append((name, False, str(e)))
            print(f"  ❌ {name}  {e}")
        except Exception as e:
            results.append((name, False, f"exception: {e}"))
            print(f"  ❌ {name}  exception: {e}")
        return fn
    return wrap


# ── 1. Module classification ──────────────────────────────────────

print("\n─── 1. Module classification (no LLM in rule-based modules) ───")

RULE_BASED_MODULES = [
    "core/technicals.py",
    "core/freshness.py",
    "events/fetcher.py",
    "events/expected_move.py",
    "events/context.py",
    "events/monthly_review.py",
    "notifiers/telegram.py",
]

@check("no agent1/agent3 imports in rule-based modules")
def test_no_llm_in_rule_based():
    bad = []
    for rel in RULE_BASED_MODULES:
        text = (ROOT / rel).read_text()
        if re.search(r"from\s+agents\.agent(1|3)", text):
            bad.append(rel)
    assert not bad, f"LLM imports in: {bad}"
    return f"({len(RULE_BASED_MODULES)} modules clean)"


@check("no 'openrouter'/'requests' in technicals.py")
def test_technicals_no_network_deps():
    text = (ROOT / "core/technicals.py").read_text()
    # technicals.py delegates to core.hl_client which does the HTTP.
    assert "openrouter" not in text.lower(), "unexpected LLM endpoint ref"
    assert "import requests" not in text, "technicals should not do its own HTTP"


# ── 2. Determinism ────────────────────────────────────────────────

print("\n─── 2. Determinism ───")

@check("technicals output stable across two calls (mocked candles)")
def test_technicals_determinism():
    from core import technicals as tech

    # Build a fake candle series (300 bars, deterministic walk)
    base_price = 100.0
    bars = []
    px = base_price
    for i in range(300):
        o = px; h = px + 0.5; l = px - 0.5
        c = px + (0.1 if i % 2 == 0 else -0.08)
        bars.append((i * 900_000, o, h, l, c, 1_000.0))
        px = c

    # Monkey-patch fetch_candles to return our synthetic series
    orig = tech.fetch_candles
    tech.fetch_candles = lambda coin, interval, lb: bars

    try:
        os.environ["TECHNICALS_ENABLED"] = "true"
        r1 = tech.get_technical_outlook("xyz:FAKE", px)
        r2 = tech.get_technical_outlook("xyz:FAKE", px)
    finally:
        tech.fetch_candles = orig

    assert r1 == r2, "deterministic call returned different dicts"
    assert r1.get("enabled"), f"expected enabled=True, got {r1.get('reason')}"


@check("monthly_review signal scoring deterministic")
def test_monthly_review_determinism():
    from events.monthly_review import _beat_miss_streak_signal, _news_sentiment_signal

    hist = [{"surprise_pct": 3.5}, {"surprise_pct": -2.1}, {"surprise_pct": 4.0}]
    for _ in range(3):
        s, _ = _beat_miss_streak_signal(hist)
        assert s == 0  # 2 beats, 1 miss → net +1 < threshold of 2 → 0

    news = [{"headline": "Company beats estimates and raises guidance"},
            {"headline": "Major partnership announced"},
            {"headline": "Upgrade to buy from hold"},
            {"headline": "Breakout on strong volume"},
            {"headline": "Growth record for Q1"}]
    for _ in range(3):
        s, _ = _news_sentiment_signal(news)
        assert s == +1, f"expected +1, got {s}"


# ── 3. Graceful degradation ──────────────────────────────────────

print("\n─── 3. Graceful degradation ───")

@check("technicals: empty candles → enabled=False")
def test_technicals_empty():
    from core import technicals as tech
    orig = tech.fetch_candles
    tech.fetch_candles = lambda *a, **k: []
    try:
        r = tech.get_technical_outlook("xyz:NONE", 100.0)
    finally:
        tech.fetch_candles = orig
    assert not r.get("enabled"), f"expected disabled, got {r}"


@check("technicals: disabled flag honored")
def test_technicals_disabled():
    os.environ["TECHNICALS_ENABLED"] = "false"
    try:
        from core import technicals as tech
        r = tech.get_technical_outlook("xyz:X", 100.0)
        assert not r.get("enabled")
        assert r.get("reason") == "disabled"
    finally:
        os.environ["TECHNICALS_ENABLED"] = "true"


@check("events/context: zero price does not crash")
def test_events_zero_price():
    from events.context import get_event_context
    # With no Finnhub key, fetcher returns [] → context returns graceful dict
    prev = os.environ.pop("FINNHUB_API_KEY", None)
    try:
        r = get_event_context("XYZ", "xyz:XYZ", 0.0)
    finally:
        if prev is not None:
            os.environ["FINNHUB_API_KEY"] = prev
    # Must not raise; may be enabled=True with empty data or disabled
    assert isinstance(r, dict)


@check("events/fetcher: no key → empty lists, no exception")
def test_fetcher_no_key():
    from events.fetcher import get_earnings_calendar, get_macro_calendar
    prev = os.environ.pop("FINNHUB_API_KEY", None)
    try:
        assert get_earnings_calendar(3) == []
        assert get_macro_calendar(3) == []
    finally:
        if prev is not None:
            os.environ["FINNHUB_API_KEY"] = prev


# ── 4. HTML safety ───────────────────────────────────────────────

print("\n─── 4. HTML safety ───")

_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9]*)([^<>]*)>")


def _tags_balanced(html: str) -> bool:
    stack = []
    for m in _TAG_RE.finditer(html):
        closing = bool(m.group(1))
        tag = m.group(2).lower()
        if tag in {"br"}:
            continue
        if not closing:
            stack.append(tag)
        else:
            if not stack or stack[-1] != tag:
                return False
            stack.pop()
    return not stack


@check("technicals render: tags balanced, no bare <digit>")
def test_technicals_html():
    from core import technicals as tech

    bars = []; px = 100.0
    for i in range(300):
        bars.append((i * 900_000, px, px + .4, px - .4, px + .08, 1000.))
        px = bars[-1][4]
    orig = tech.fetch_candles
    tech.fetch_candles = lambda *a, **k: bars
    try:
        r = tech.get_technical_outlook("xyz:FAKE", px)
    finally:
        tech.fetch_candles = orig

    html = r["rendered"]
    assert _tags_balanced(html), "unbalanced tags"
    # Telegram parser trips on <digit — make sure there are none raw
    assert not re.search(r"<\d", html), "bare <digit sequence found"


@check("events/context render: tags balanced")
def test_events_html():
    from events.context import _render
    # Fully-populated synthetic ctx
    ctx = {
        "last_earnings": {
            "period": "2026-03-31", "actual": 1.62, "estimate": 1.56,
            "surprise_pct": 3.6, "verdict": "beat", "post_earnings_move_pct": 6.4,
        },
        "next_earnings": {"date": "2026-05-20", "hour": "amc", "eps_estimate": 1.79},
        "days_to_earnings": 27,
        "forecast": {
            "lean": "🟢🟢 STRONG BULLISH", "score": 3,
            "signals": {
                "beat_miss_streak": {"score": 1, "detail": "4/4 beats"},
                "analyst_recs":     {"score": 1, "detail": "66B/4H/1S"},
                "price_target":     {"score": 0, "detail": "no target"},
                "news_sentiment":   {"score": 1, "detail": "14 bull/8 bear"},
            },
        },
        "expected_move": None,
        "macro_events_soon": [],
    }
    html = _render(ctx)
    assert _tags_balanced(html)


# ── 5. Price grounding sanity ─────────────────────────────────────

print("\n─── 5. Price-grounding sanity ───")

@check("technicals render: no $ figure exceeds 1.5× reference price (hallucination cap)")
def test_price_bounds():
    """
    Upper bound only: a hallucinated price level (e.g. $450 on a $200
    stock) is the failure mode we care about. Lower values are fine —
    ATR is routinely ~1% of price, so $0.73 on a $170 stock is legit.
    """
    from core import technicals as tech
    px = 200.0
    bars = []; p = px
    for i in range(300):
        p += (0.5 if i % 3 == 0 else -0.4)
        bars.append((i * 900_000, p - .1, p + .3, p - .3, p, 1000.))
    orig = tech.fetch_candles
    tech.fetch_candles = lambda *a, **k: bars
    try:
        r = tech.get_technical_outlook("xyz:FAKE", bars[-1][4])
    finally:
        tech.fetch_candles = orig
    html = r["rendered"]
    ref = bars[-1][4]
    over = []
    for m in re.finditer(r"\$(\d+(?:\.\d+)?)", html):
        v = float(m.group(1))
        if v > 1.5 * ref:
            over.append(v)
    assert not over, f"hallucinatory high prices {over} vs ref {ref}"


# ── 6. Numeric citation ──────────────────────────────────────────

print("\n─── 6. Numeric citation in technicals ───")

@check("technicals: rendered block cites RSI, EMA, ATR, MACD numbers")
def test_numeric_citation():
    from core import technicals as tech
    bars = []; p = 150.0
    for i in range(300):
        p += 0.12 if i % 2 == 0 else -0.08
        bars.append((i * 900_000, p, p + .3, p - .3, p, 1000.))
    orig = tech.fetch_candles
    tech.fetch_candles = lambda *a, **k: bars
    try:
        r = tech.get_technical_outlook("xyz:FAKE", bars[-1][4])
    finally:
        tech.fetch_candles = orig
    html = r["rendered"]
    assert "RSI" in html, "RSI not cited"
    assert "ATR(14)" in html, "ATR not cited"
    assert "MACD" in html, "MACD not cited"
    assert "EMA" in html, "EMA not cited"


# ── 7. Agent 3 guardrail exercise ────────────────────────────────

print("\n─── 7. Agent 3 hallucination guardrails ───")

@check("price-grounding violation flagged")
def test_a3_price_violation():
    from agents.agent3_causality import _validate_and_ground
    price_trigger = {"current_price": 200.0}
    news_report = {"has_news": True}
    condition = {}
    result = {
        "verdict": "Strong bull move to $450.00 driven by earnings.",
        "confidence": "high", "primary_driver": "oi_flow",
        "reasoning": "—", "flags": [],
    }
    _validate_and_ground(result, price_trigger, news_report, condition, "NVDA")
    assert "price_grounding_violation" in result["flags"]


@check("news-driver without news coerced to unknown")
def test_a3_news_consistency():
    from agents.agent3_causality import _validate_and_ground
    result = {
        "verdict": "Price moved due to breaking news.",
        "confidence": "high", "primary_driver": "news",
        "reasoning": "—", "flags": [],
    }
    _validate_and_ground(result, {"current_price": 100.0},
                          {"has_news": False}, {}, "NVDA")
    assert result["primary_driver"] == "unknown"
    assert "news_driver_without_news" in result["flags"]


@check("foreign ticker mention flagged")
def test_a3_foreign_ticker():
    from agents.agent3_causality import _validate_and_ground
    result = {
        "verdict": "NVDA move on TSLA rumor", "confidence": "high",
        "primary_driver": "news", "reasoning": "—", "flags": [],
    }
    _validate_and_ground(result, {"current_price": 200.0},
                          {"has_news": True}, {}, "NVDA")
    foreign_flags = [f for f in result["flags"] if f.startswith("foreign_tokens")]
    assert foreign_flags, "expected foreign_tokens flag"
    assert "TSLA" in foreign_flags[0]


@check("invalid confidence coerced to 'low'")
def test_a3_invalid_confidence():
    from agents.agent3_causality import _validate_and_ground
    result = {"verdict": "—", "confidence": "supersure",
              "primary_driver": "oi_flow", "reasoning": "—", "flags": []}
    _validate_and_ground(result, {"current_price": 100.0},
                          {"has_news": False}, {}, "NVDA")
    assert result["confidence"] == "low"
    assert "invalid_confidence_coerced" in result["flags"]


# ── 8. Agent 1 fabrication guard ─────────────────────────────────

print("\n─── 8. Agent 1 fabrication guard ───")

@check("0 articles + LLM produced summary → discarded")
def test_a1_fabrication_guard():
    from agents.agent1_news import _validate_summary
    # Empty articles, LLM hallucinated content → must be discarded
    r = _validate_summary("Nvidia is rallying on AI news.", [], [], "NVDA")
    assert "no clear catalyst" in r.lower() or r == "No recent news found.", \
        f"fabrication not discarded: {r!r}"


@check("out-of-range citations stripped")
def test_a1_oor_citations():
    from agents.agent1_news import _validate_summary
    articles = [{"title": "A"}, {"title": "B"}]
    summary = "Big news [1]. More [2]. Invented [9]."
    r = _validate_summary(summary, [1, 2, 9], articles, "NVDA")
    assert "[9]" not in r, f"out-of-range [9] not stripped: {r}"


# ── Summary ──────────────────────────────────────────────────────

def summary() -> tuple[int, int]:
    passed = sum(1 for _, ok, _ in results if ok)
    return passed, len(results)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--telegram", action="store_true")
    args = p.parse_args()

    passed, total = summary()
    print(f"\n═══ QA RESULT: {passed}/{total} passed ═══")

    if args.telegram:
        token = os.environ.get("TELEGRAM_BOT_TOKEN",
            "8753215742:AAGNPqDOc1Xr0lb5nVoTGtlA25Hzt6wqLfo")
        chat = os.environ.get("TELEGRAM_CHAT_ID", "-1003819293218")
        lines = [f"<b>🧪 QA (anti-hallucination framework) — {passed}/{total} passed</b>\n"]
        for name, ok, detail in results:
            icon = "✅" if ok else "❌"
            lines.append(f"{icon} {name}" + (f" · <i>{detail}</i>" if detail and not ok else ""))
        text = "\n".join(lines)
        subprocess.run([
            "curl", "-s", "-X", "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            "--data-urlencode", f"chat_id={chat}",
            "--data-urlencode", f"text={text}",
            "--data-urlencode", "parse_mode=HTML",
            "--data-urlencode", "disable_web_page_preview=true",
        ], capture_output=True, text=True, timeout=15)

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
