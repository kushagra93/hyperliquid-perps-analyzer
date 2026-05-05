# notifiers/pn_compact.py
# ─────────────────────────────────────────────────────────────────
# Compact PN format — designed for the dedicated PN channel.
#
# Hard constraints (enforced, not just advised):
#   • Title  ≤ 50 chars  (1 mobile line)
#   • Body   ≤ 80 chars  (2 mobile lines)
#   • Total  ≤ 130 chars
#
# Tone: Hinglish-crisp. Action verbs first. No fluff. No
# "consider" / "may potentially" / "watch closely". The user has
# 1 second of attention; the PN tells them what + why + do-what.
#
# Three stages of a single event (timeline waves):
#   T+0       break_news    catalyst + first move + immediate setup
#   T+15min   cascade       follow-through + level test
#   T+2hr     repricing     equilibrium + next setup
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

TITLE_MAX = 50
BODY_MAX = 80
TOTAL_MAX = 130


def _truncate(text: str, max_chars: int) -> str:
    """Hard truncate at char boundary, ellipsize cleanly."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    # Drop trailing words that would push us over
    return text[: max_chars - 1].rstrip(" ,.;:") + "…"


def _enforce(card: dict) -> dict:
    """Apply hard limits and assert total."""
    card["title"] = _truncate(card["title"], TITLE_MAX)
    card["body"] = _truncate(card["body"], BODY_MAX)
    card["total_chars"] = len(card["title"]) + len(card["body"]) + 1
    if card["total_chars"] > TOTAL_MAX:
        # Trim body further to fit
        card["body"] = _truncate(card["body"], TOTAL_MAX - len(card["title"]) - 1)
        card["total_chars"] = len(card["title"]) + len(card["body"]) + 1
    return card


# ── Stage 1: T+0 break news (intel-driven, no templates) ────────

def format_break_news(*, symbol: str, move_pct: float,
                       catalyst: str | None = None,
                       hl_asset: str | None = None,
                       condition_id: str = "",
                       funding: float = 0.0,
                       oi_change_pct: float | None = None,
                       cluster_tilt: str | None = None) -> dict:
    """
    T+0: title from catalyst + move; body from LIVE INTELLIGENCE
    (vol z-score, ATR ratio, VWAP-relative, OI/funding, cluster tilt).
    """
    from notifiers.compact_intel import (
        IntelInputs, fetch_intel_signals, build_intel_body
    )
    intel = fetch_intel_signals(symbol, hl_asset)
    inp = IntelInputs(
        symbol=symbol, move_pct=move_pct,
        condition_id=condition_id, funding=funding,
        oi_change_pct=oi_change_pct,
        news_headline=catalyst, cluster_tilt=cluster_tilt,
        volume_zscore=intel.get("volume_zscore"),
        atr_ratio=intel.get("atr_ratio"),
        near_vwap=intel.get("near_vwap"),
        range_compression=intel.get("range_compression", False),
    )
    body = build_intel_body(inp)

    emoji = "🔴" if move_pct < -0.3 else ("🟢" if move_pct > 0.3 else "⚪")
    cat = (catalyst or "Move").strip()
    if len(cat) > 22:
        cat = cat[:21] + "…"
    title = f"{emoji} {cat} · {symbol} {move_pct:+.1f}%"
    return _enforce({"stage": "break", "title": title, "body": body})


# ── Stage 2: T+15min cascade ────────────────────────────────────

def format_cascade(*, symbol: str, move_pct: float,
                    liquidation_usd_m: float | None = None,
                    level: float | None = None,
                    hl_asset: str | None = None,
                    condition_id: str = "",
                    funding: float = 0.0,
                    oi_change_pct: float | None = None) -> dict:
    """
    T+15: cascade title (liquidation/VWAP/level) + intel-driven body.
    """
    from notifiers.compact_intel import (
        IntelInputs, fetch_intel_signals, build_intel_body
    )
    intel = fetch_intel_signals(symbol, hl_asset)

    if move_pct < 0:
        emoji = "💀" if liquidation_usd_m and liquidation_usd_m >= 5 else "📉"
    else:
        emoji = "🚀" if move_pct > 1.5 else "📈"

    vwap = intel.get("vwap")
    if liquidation_usd_m:
        title = f"{emoji} ${liquidation_usd_m:.0f}M liq · {symbol} {move_pct:+.1f}%"
    elif level:
        title = f"{emoji} {symbol} ${level:.0f} test · {move_pct:+.1f}%"
    elif vwap:
        title = f"{emoji} {symbol} ${vwap:.0f} VWAP · {move_pct:+.1f}%"
    else:
        title = f"{emoji} {symbol} cascade · {move_pct:+.1f}%"

    inp = IntelInputs(
        symbol=symbol, move_pct=move_pct,
        condition_id=condition_id, funding=funding,
        oi_change_pct=oi_change_pct,
        volume_zscore=intel.get("volume_zscore"),
        atr_ratio=intel.get("atr_ratio"),
        near_vwap=intel.get("near_vwap"),
        range_compression=intel.get("range_compression", False),
    )
    body = build_intel_body(inp)
    return _enforce({"stage": "cascade", "title": title, "body": body})


# ── Stage 3: T+2hr repricing ────────────────────────────────────

def format_repricing(*, symbol: str, next_event: str | None = None,
                      net_move_pct: float | None = None,
                      hl_asset: str | None = None,
                      condition_id: str = "",
                      funding: float = 0.0) -> dict:
    """T+2hr: equilibrium read + intel-driven body + next event tag."""
    from notifiers.compact_intel import (
        IntelInputs, fetch_intel_signals, _facts
    )
    intel = fetch_intel_signals(symbol, hl_asset)

    if net_move_pct is not None:
        title = f"📊 {symbol} settled · {net_move_pct:+.1f}% net"
    else:
        title = f"📊 {symbol} repriced · equilibrium"

    inp = IntelInputs(
        symbol=symbol, move_pct=net_move_pct or 0.0,
        condition_id=condition_id, funding=funding,
        volume_zscore=intel.get("volume_zscore"),
        atr_ratio=intel.get("atr_ratio"),
        near_vwap=intel.get("near_vwap"),
        range_compression=intel.get("range_compression", False),
    )
    facts = _facts(inp)[:2]
    parts = list(facts)
    if next_event:
        nxt = next_event.strip()
        if len(nxt) > 22: nxt = nxt[:21] + "…"
        parts.append(f"Next: {nxt}")
    if not parts:
        parts.append(f"{(net_move_pct or 0):+.1f}% net")
    body = " · ".join(parts) + ". Watch."
    return _enforce({"stage": "repricing", "title": title, "body": body})


# ── Wave: 3 PNs for one event ───────────────────────────────────

def build_wave(*, symbol: str, move_pct: float,
                catalyst: str | None = None,
                level: float | None = None,
                liquidation_usd_m: float | None = None,
                next_event: str | None = None,
                net_move_pct: float | None = None,
                hl_asset: str | None = None,
                condition_id: str = "",
                funding: float = 0.0,
                oi_change_pct: float | None = None,
                cluster_tilt: str | None = None) -> list[dict]:
    """
    Pre-build all 3 stages with live intelligence threaded through.
    Each stage re-fetches its own intel from HL when called, so we
    pick up the latest volume / VWAP / ATR per fire.
    """
    return [
        format_break_news(symbol=symbol, move_pct=move_pct, catalyst=catalyst,
                           hl_asset=hl_asset, condition_id=condition_id,
                           funding=funding, oi_change_pct=oi_change_pct,
                           cluster_tilt=cluster_tilt),
        format_cascade(symbol=symbol, move_pct=move_pct,
                        liquidation_usd_m=liquidation_usd_m, level=level,
                        hl_asset=hl_asset, condition_id=condition_id,
                        funding=funding, oi_change_pct=oi_change_pct),
        format_repricing(symbol=symbol, next_event=next_event,
                          net_move_pct=net_move_pct, hl_asset=hl_asset,
                          condition_id=condition_id, funding=funding),
    ]


# ── Telegram payload ────────────────────────────────────────────

def to_telegram(card: dict) -> str:
    """HTML-formatted Telegram payload, mobile-optimized."""
    return f"<b>{card['title']}</b>\n{card['body']}"


if __name__ == "__main__":
    # Smoke test
    wave = build_wave(
        symbol="AAPL", move_pct=-1.2,
        catalyst="Trump tariff 25%",
        level=168.0,
        liquidation_usd_m=18.0,
        next_event="Congress vote",
        net_move_pct=-1.8,
    )
    for stage_card in wave:
        print(f"\n[{stage_card['stage']}]  {stage_card['total_chars']} chars")
        print(to_telegram(stage_card))
