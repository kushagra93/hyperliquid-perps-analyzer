#!/usr/bin/env python3
"""
tests/test_pn_compact.py
────────────────────────────────────────────────────────────────────
Hard-enforce the compact PN size constraints. Run as:
  python3 tests/test_pn_compact.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from notifiers.pn_compact import (
    TITLE_MAX, BODY_MAX, TOTAL_MAX,
    format_break_news, format_cascade, format_repricing, build_wave,
)

results: list[tuple[str, bool, str]] = []


def check(name: str, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"  ✅ {name}")
    except AssertionError as e:
        results.append((name, False, str(e)))
        print(f"  ❌ {name}: {e}")
    except Exception as e:
        results.append((name, False, repr(e)))
        print(f"  ❌ {name}: {e}")


def t_break_size():
    c = format_break_news(symbol="AAPL", move_pct=-1.2, catalyst="Trump tariff 25%")
    assert len(c["title"]) <= TITLE_MAX, f"title {len(c['title'])} > {TITLE_MAX}"
    assert len(c["body"]) <= BODY_MAX, f"body {len(c['body'])} > {BODY_MAX}"
    assert c["total_chars"] <= TOTAL_MAX


def t_break_long_catalyst_truncated():
    c = format_break_news(symbol="NVDA", move_pct=2.5,
                           catalyst="Multi-faceted earnings beat with hyperscaler signals")
    assert len(c["title"]) <= TITLE_MAX


def t_cascade_with_liq_size():
    c = format_cascade(symbol="MSTR", move_pct=-3.4, liquidation_usd_m=45,
                        level=170)
    assert len(c["title"]) <= TITLE_MAX, f"title {len(c['title'])}"
    assert len(c["body"]) <= BODY_MAX


def t_cascade_no_liq_size():
    c = format_cascade(symbol="HOOD", move_pct=1.8, level=85)
    assert len(c["title"]) <= TITLE_MAX
    assert len(c["body"]) <= BODY_MAX


def t_repricing_size():
    c = format_repricing(symbol="TSLA", next_event="FOMC tomorrow",
                          net_move_pct=-1.8)
    assert len(c["title"]) <= TITLE_MAX
    assert len(c["body"]) <= BODY_MAX


def t_repricing_long_event():
    c = format_repricing(symbol="GOOGL",
                          next_event="Q4 earnings release plus guidance update",
                          net_move_pct=0.5)
    assert len(c["title"]) <= TITLE_MAX
    assert len(c["body"]) <= BODY_MAX


def t_wave_returns_3_stages():
    wave = build_wave(symbol="AAPL", move_pct=-1.2, catalyst="Tariff",
                       level=168, liquidation_usd_m=18,
                       next_event="Congress vote", net_move_pct=-1.8)
    assert len(wave) == 3
    stages = [c["stage"] for c in wave]
    assert stages == ["break", "cascade", "repricing"]


def t_wave_total_under_400_chars():
    """All 3 PNs combined fit a single mobile screen."""
    wave = build_wave(symbol="AAPL", move_pct=-1.2, catalyst="Trump tariff 25%",
                       level=168, liquidation_usd_m=18, next_event="Congress vote")
    total = sum(c["total_chars"] for c in wave)
    assert total <= 400, f"3 cards total {total} chars (>400)"


def t_break_emoji_signed():
    bear = format_break_news(symbol="X", move_pct=-2.0)
    bull = format_break_news(symbol="X", move_pct=+2.0)
    assert "🔴" in bear["title"]
    assert "🟢" in bull["title"]


def t_extreme_inputs():
    """Pathological inputs shouldn't blow the limits."""
    long_sym = "VERYLONGSYMBOLNAMEFORTESTING"
    c = format_break_news(symbol=long_sym, move_pct=-9.99,
                           catalyst="A" * 100)
    assert len(c["title"]) <= TITLE_MAX, f"title overflow: {len(c['title'])}"
    assert len(c["body"]) <= BODY_MAX


print("\n═══ COMPACT PN HARD-LIMIT TESTS ═══\n")
for name, fn in [
    ("break: size limits", t_break_size),
    ("break: long catalyst truncated", t_break_long_catalyst_truncated),
    ("cascade: with liquidation size", t_cascade_with_liq_size),
    ("cascade: no liquidation size", t_cascade_no_liq_size),
    ("repricing: size limits", t_repricing_size),
    ("repricing: long event truncated", t_repricing_long_event),
    ("wave: returns 3 ordered stages", t_wave_returns_3_stages),
    ("wave: combined total ≤ 400 chars", t_wave_total_under_400_chars),
    ("break: emoji follows direction", t_break_emoji_signed),
    ("extreme inputs don't overflow", t_extreme_inputs),
]:
    check(name, fn)

passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
print(f"\nResult: {passed}/{total} passed")
sys.exit(0 if passed == total else 1)
