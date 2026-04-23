# events/context.py
# ─────────────────────────────────────────────────────────────────
# Thin facade for ticker_worker: given (symbol, coin, price), return a
# single `event_context` dict with:
#   - next earnings date + days away
#   - expected move ± band around current price
#   - relevant upcoming high-impact macro events
#   - a pre-rendered human-readable string for Telegram
#
# Everything is optional: if FINNHUB_API_KEY is unset, returns a
# `{"enabled": False, ...}` dict the caller can ignore.
#
# Env:
#   EVENTS_CONTEXT_ENABLED   default true — master switch
#   EVENTS_PRE_EARNINGS_DAYS default 5 — show earnings only when within N days
#   EVENTS_MACRO_HORIZON_DAYS default 3 — show macro only when within N days
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
from datetime import datetime, date, timezone
from pathlib import Path

from events.fetcher import upcoming_for_symbol
from events.expected_move import expected_move_for_event
from events.monthly_review import _earnings_move_pct, forecast_for_ticker

logger = logging.getLogger(__name__)


def _env_bool(key: str, default: bool) -> bool:
    v = os.environ.get(key, "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _compute_tags(next_earnings, days_to_earnings, forecast, expected_move) -> list[dict]:
    """
    Return a short list of tag dicts summarizing expectations for the
    upcoming earnings release. Each dict has: {kind, code, label, emoji}.
    Downstream (Telegram, Sheets, JSONL) sees structured `code` values
    and can render or filter consistently.
    """
    tags: list[dict] = []
    if not next_earnings:
        return tags

    # Expectation tag from forward-lean score (-4..+4)
    score = (forecast or {}).get("score")
    if score is not None:
        if score >= 3:
            tags.append({"kind": "expectation", "code": "beat_high",
                          "emoji": "🔥", "label": "HIGH-CONVICTION BEAT"})
        elif score >= 1:
            tags.append({"kind": "expectation", "code": "beat_lean",
                          "emoji": "📈", "label": "BEAT LEAN"})
        elif score <= -3:
            tags.append({"kind": "expectation", "code": "miss_high",
                          "emoji": "🚨", "label": "HIGH MISS RISK"})
        elif score <= -1:
            tags.append({"kind": "expectation", "code": "miss_lean",
                          "emoji": "📉", "label": "MISS LEAN"})
        else:
            tags.append({"kind": "expectation", "code": "mixed",
                          "emoji": "⚖️", "label": "MIXED / TOO CLOSE"})

    # Urgency tag
    dte = days_to_earnings
    if dte is not None:
        if dte <= 1:
            tags.append({"kind": "urgency", "code": "imminent",
                          "emoji": "⚡", "label": "IMMINENT (≤1d)"})
        elif dte <= 7:
            tags.append({"kind": "urgency", "code": "this_week",
                          "emoji": "🕐", "label": "THIS WEEK"})
        elif dte <= 30:
            tags.append({"kind": "urgency", "code": "this_month",
                          "emoji": "📅", "label": "THIS MONTH"})
        else:
            tags.append({"kind": "urgency", "code": "distant",
                          "emoji": "🌑", "label": f"IN {dte}d"})

    # Volatility tag (from expected-move band if computed)
    em_pct = (expected_move or {}).get("expected_pct")
    if em_pct is not None:
        if em_pct >= 8:
            tags.append({"kind": "volatility", "code": "high",
                          "emoji": "🌋", "label": f"HIGH VOL (±{em_pct:.1f}%)"})
        elif em_pct >= 4:
            tags.append({"kind": "volatility", "code": "medium",
                          "emoji": "🌊", "label": f"MEDIUM VOL (±{em_pct:.1f}%)"})
        else:
            tags.append({"kind": "volatility", "code": "low",
                          "emoji": "💤", "label": f"LOW VOL (±{em_pct:.1f}%)"})

    return tags


def _macro_within(macro_events: list, max_days: int, us_only: bool = True) -> list:
    """
    Keep only macro events happening within max_days from today.

    By default restrict to US (the tracked universe is US stocks).
    The fetcher already applied a HIGH_IMPACT keyword allowlist so
    this is a second filter on country to strip foreign PMI noise.
    """
    kept = []
    today = date.today()
    for e in macro_events:
        if us_only and (e.get("country") or "").upper() not in ("US", "USA"):
            continue
        t = e.get("time") or ""
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00")).date()
        except Exception:
            try:
                d = datetime.strptime(t[:10], "%Y-%m-%d").date()
            except Exception:
                continue
        dte = (d - today).days
        if 0 <= dte <= max_days:
            e2 = dict(e); e2["days_away"] = dte
            kept.append(e2)
    kept.sort(key=lambda x: x.get("days_away", 99))
    return kept


def _render(ctx: dict) -> str:
    parts = []

    # ── Last earnings ──
    last = ctx.get("last_earnings")
    if last:
        verdict = last.get("verdict", "in-line")
        emoji = {"beat": "🟢", "miss": "🔴", "in-line": "⚪"}.get(verdict, "⚪")
        sp = last.get("surprise_pct")
        sp_s = f"{sp:+.2f}%" if sp is not None else "n/a"
        move = last.get("post_earnings_move_pct")
        move_s = f"{move:+.2f}%" if move is not None else "n/a"
        actual = last.get("actual"); est = last.get("estimate")
        parts.append(
            f"{emoji} <b>Last earnings {last.get('period')}</b> — {verdict.upper()}  "
            f"surprise {sp_s} · T±1 move {move_s}  "
            f"<i>(actual {actual} vs est {est})</i>"
        )

    # ── Upcoming earnings + forecast + expected move ──
    ner = ctx.get("next_earnings")
    dte = ctx.get("days_to_earnings")
    if ner and dte is not None and dte >= 0:
        hour_map = {"bmo": "before open", "amc": "after close", "dmh": "during market"}
        when = hour_map.get(ner.get("hour", ""), ner.get("hour", ""))
        when_str = f" ({when})" if when else ""
        eps = ner.get("eps_estimate")
        eps_str = f" · EPS est <code>{eps}</code>" if eps is not None else ""
        parts.append(f"📅 <b>Next earnings in {dte}d</b> — {ner.get('date')}{when_str}{eps_str}")

        # Expectation tags line (structured + rendered together)
        tags = ctx.get("tags") or []
        if tags:
            tag_str = "  ·  ".join(f"{t['emoji']} <b>{t['label']}</b>" for t in tags)
            parts.append(f"   🏷️ {tag_str}")

        fcst = ctx.get("forecast") or {}
        if fcst.get("lean"):
            parts.append(f"   <b>Lean:</b> {fcst['lean']}  score <code>{fcst.get('score', 0):+d}/4</code>")
            sigs = fcst.get("signals") or {}
            for name in ("beat_miss_streak", "analyst_recs", "price_target", "news_sentiment"):
                sig = sigs.get(name) or {}
                sc = sig.get("score", 0)
                e_ico = "🟢" if sc > 0 else "🔴" if sc < 0 else "⚪"
                label = name.replace("_", " ")
                detail = sig.get("detail", "—")
                parts.append(f"     {e_ico} {label}: <i>{detail}</i>")

        em = ctx.get("expected_move") or {}
        if em.get("expected_pct"):
            parts.append(
                f"   <b>Expected move:</b> ±{em['expected_pct']:.2f}% "
                f"(${em['lower_band']:.2f} – ${em['upper_band']:.2f}) "
                f"<i>[stat {em.get('statistical_pct', 0):.1f}% · hist {em.get('historical_earnings_pct') or '—'}% over {em.get('historical_n', 0)}q]</i>"
            )
    elif last is None:
        parts.append("<i>No earnings data available for this ticker.</i>")

    # ── Macro nearby ──
    macro = ctx.get("macro_events_soon") or []
    for e in macro[:3]:
        label = e.get("event") or "event"
        d_str = f"in {e.get('days_away')}d" if e.get("days_away") is not None else "soon"
        parts.append(f"🏛️ <b>{label}</b> — {e.get('country') or '?'} {d_str}")

    return "\n".join(parts) if parts else ""


def get_event_context(symbol: str, hl_asset: str, current_price: float) -> dict:
    """
    Called by ticker_worker when assembling alert payload. Never raises;
    returns `{"enabled": False}` on any error so alerting is not blocked.
    """
    if not _env_bool("EVENTS_CONTEXT_ENABLED", True):
        return {"enabled": False, "reason": "disabled"}
    try:
        pre_er = _env_int("EVENTS_PRE_EARNINGS_DAYS", 5)
        macro_horizon = _env_int("EVENTS_MACRO_HORIZON_DAYS", 3)
        earnings_horizon = _env_int("EVENTS_EARNINGS_HORIZON_DAYS", 90)

        # Use a long lookahead so quarterly cadence is always captured
        bundle = upcoming_for_symbol(symbol, days=earnings_horizon)
        hist = bundle.get("earnings_history") or []

        # ── Always include: last earnings + realized move ──
        last_ctx = None
        if hist:
            last = hist[0]
            sp = last.get("surprise_pct")
            verdict = "beat" if (sp or 0) > 0.5 else "miss" if (sp or 0) < -0.5 else "in-line"
            try:
                move = _earnings_move_pct(hl_asset, last.get("period") or "")
            except Exception:
                move = None
            last_ctx = {
                "period": last.get("period"),
                "actual": last.get("eps_actual"),
                "estimate": last.get("eps_estimate"),
                "surprise_pct": sp,
                "verdict": verdict,
                "post_earnings_move_pct": round(move, 2) if move is not None else None,
            }

        # ── Always include: upcoming earnings + forward lean ──
        ner = bundle.get("next_earnings")
        dte = bundle.get("days_to_earnings")
        forecast = None
        em = None
        if ner:
            try:
                forecast = forecast_for_ticker(
                    symbol, hl_asset, current_price, ner.get("date")
                )
            except Exception as fe:
                logger.warning(f"[events/{symbol}] forecast failed: {fe}")
            # Expected move only when sufficiently close — uses HL ATR math
            if dte is not None and 0 <= dte <= pre_er:
                em = expected_move_for_event(hl_asset, current_price, dte, hist)

        ctx: dict = {
            "enabled": True,
            "symbol": symbol,
            "last_earnings": last_ctx,
            "next_earnings": ner,
            "days_to_earnings": dte,
            "forecast": forecast,
            "expected_move": em,
            "tags": _compute_tags(ner, dte, forecast, em),
            "macro_events_soon": _macro_within(
                bundle.get("macro_events") or [], macro_horizon
            ),
        }
        ctx["rendered"] = _render(ctx)
        return ctx
    except Exception as e:
        logger.warning(f"[events/{symbol}] get_event_context failed: {e}")
        return {"enabled": False, "reason": str(e)}
