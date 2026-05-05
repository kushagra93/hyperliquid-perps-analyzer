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


# ── Stage 1: T+0 break news ─────────────────────────────────────

_BREAK_BODIES_BEAR = [
    "Supply hit. Short zone.",
    "Sellers in. Fade rallies.",
    "Bear setup live. Tight stops.",
    "Distribution. Short pullbacks.",
]
_BREAK_BODIES_BULL = [
    "Demand bid. Long dips.",
    "Buyers in. Trail stops.",
    "Bull setup live. Pyramid.",
    "Accumulation. Buy zone.",
]
_BREAK_BODIES_FLAT = [
    "Squeeze building. Watch breakout.",
    "Coiling. No trade till resolved.",
]


def format_break_news(*, symbol: str, move_pct: float,
                       catalyst: str | None = None) -> dict:
    """T+0: catalyst hits, first move printed, immediate setup."""
    emoji = "🔴" if move_pct < -0.3 else ("🟢" if move_pct > 0.3 else "⚪")
    cat = (catalyst or "Move").strip()
    if len(cat) > 20:
        cat = cat[:19] + "…"
    title = f"{emoji} {cat} · {symbol} {move_pct:+.1f}%"

    if move_pct < -0.3:
        body = _BREAK_BODIES_BEAR[abs(int(move_pct * 10)) % len(_BREAK_BODIES_BEAR)]
    elif move_pct > 0.3:
        body = _BREAK_BODIES_BULL[abs(int(move_pct * 10)) % len(_BREAK_BODIES_BULL)]
    else:
        body = _BREAK_BODIES_FLAT[0]

    return _enforce({"stage": "break", "title": title, "body": body})


# ── Stage 2: T+15min cascade ────────────────────────────────────

def format_cascade(*, symbol: str, move_pct: float,
                    liquidation_usd_m: float | None = None,
                    level: float | None = None) -> dict:
    """T+15: follow-through, liquidation cascade, level test."""
    if move_pct < 0:
        emoji = "💀" if liquidation_usd_m and liquidation_usd_m >= 5 else "📉"
    else:
        emoji = "🚀" if move_pct > 1.5 else "📈"

    if liquidation_usd_m:
        title = f"{emoji} ${liquidation_usd_m:.0f}M liq · {symbol} {move_pct:+.1f}%"
    elif level:
        title = f"{emoji} {symbol} ${level:.0f} test · {move_pct:+.1f}%"
    else:
        title = f"{emoji} {symbol} cascade · {move_pct:+.1f}%"

    if move_pct < 0:
        if level:
            body = f"Support {level:.0f} testing. Dip buy or fade?"
        else:
            body = "Bears in control. Wait for capitulation."
    else:
        if level:
            body = f"Resistance {level:.0f} cleared. Trail tight."
        else:
            body = "Bulls extending. Pyramid into strength."

    return _enforce({"stage": "cascade", "title": title, "body": body})


# ── Stage 3: T+2hr repricing ────────────────────────────────────

def format_repricing(*, symbol: str, next_event: str | None = None,
                      net_move_pct: float | None = None) -> dict:
    """T+2hr: equilibrium found, next catalyst on watch."""
    if net_move_pct is not None:
        title = f"📊 {symbol} settled · {net_move_pct:+.1f}% net"
    else:
        title = f"📊 {symbol} repriced · equilibrium in"

    nxt = (next_event or "next print").strip()
    if len(nxt) > 28:
        nxt = nxt[:27] + "…"
    body = f"New range live. Next: {nxt}. Swing setup ready."

    return _enforce({"stage": "repricing", "title": title, "body": body})


# ── Wave: 3 PNs for one event ───────────────────────────────────

def build_wave(*, symbol: str, move_pct: float,
                catalyst: str | None = None,
                level: float | None = None,
                liquidation_usd_m: float | None = None,
                next_event: str | None = None,
                net_move_pct: float | None = None) -> list[dict]:
    """
    Pre-build all 3 stages for this event. The dispatcher decides
    when to fire each one.
    """
    return [
        format_break_news(symbol=symbol, move_pct=move_pct, catalyst=catalyst),
        format_cascade(symbol=symbol, move_pct=move_pct,
                        liquidation_usd_m=liquidation_usd_m, level=level),
        format_repricing(symbol=symbol, next_event=next_event,
                          net_move_pct=net_move_pct),
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
