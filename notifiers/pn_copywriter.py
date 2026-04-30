# notifiers/pn_copywriter.py
# ─────────────────────────────────────────────────────────────────
# Generates short, lock-screen-friendly push-notification copy in
# the voice of Indian consumer brands (Cred / Zerodha / Zomato /
# Groww / Boat) — irreverent, punchy, occasionally meme-fluent —
# tuned for Indian retail traders touching US markets.
#
# Constraints:
#   - Headline ≤ 100 chars (most Android/iOS lock-screen previews
#     truncate around there).
#   - Body ≤ 3 short lines.
#   - Always carries: ticker, direction, magnitude, action verb,
#     and one risk/disclaimer beat (we are not in the business
#     of getting traders blown up).
#
# Variety mechanics:
#   - Templates are bucketed by condition (C1/C2/C3/C4) and
#     intensity (small / big / massive move).
#   - Picker is seeded by hash(ticker | date | condition) so the
#     same signal yields a consistent voice within a day, but
#     different days rotate.
#   - Macro / cultural / political hooks attach optionally —
#     e.g. when a known macro print is within EVENT_HORIZON_HOURS,
#     when a US holiday lands, or seasonally (IPL, Diwali, Holi).
#   - Disclaimer line is mandatory on every PN.
#
# Output shape:
#   {
#     "headline": "🚀 NVDA +3.2% — chips popping like Diwali. Look in.",
#     "body":     "C1 score 92/100 · ⭐⭐⭐⭐⭐\\n"
#                 "stop -1.5% · TP +2% · 2-3% size",
#     "hashtags": "#NVDA #C1 #PN",
#     "full":     "{headline}\\n\\n{body}\\n\\n{hashtags}"
#   }
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import hashlib
import random
from datetime import date, datetime, timezone, timedelta
from typing import Any

IST = timezone(timedelta(hours=5, minutes=30))


# ── Template library ─────────────────────────────────────────────
# Tone notes per bucket:
#   C1_small  — playful, confidence-building
#   C1_big    — celebratory, rocket emojis OK, hype
#   C1_huge   — controlled awe, "don't FOMO" undertone
#   C2_small  — measured caution
#   C2_big    — dark-humour bear
#   C2_huge   — somber but actionable
#   C3        — "nothing to see here", dry
#   C4        — "trap watch", knowing
#   pn_only   — used only for the daily 1-PN slot, signature ones

TEMPLATES = {
    "C1_small": [
        "🟢 {sym} up {pct}%. Calm bull, slow tea sip.",
        "🟢 {sym} +{pct}% — the kind of green CAs ignore.",
        "📈 {sym} +{pct}%. Boring is profitable.",
        "🟢 {sym} +{pct}%. SIP on steroids.",
    ],
    "C1_big": [
        "🚀 {sym} +{pct}% — chips popping like Diwali.",
        "🔥 {sym} flying +{pct}%. Don't pour the chai yet.",
        "💥 {sym} +{pct}%. Naseeb chamak gaya.",
        "🚀 {sym} +{pct}% — your CA wants a word.",
        "🔥 {sym} +{pct}% — this is not financial advice. It's a celebration.",
    ],
    "C1_huge": [
        "🚨 {sym} +{pct}% — extreme rip. Don't FOMO. Wait the pullback.",
        "💎 {sym} +{pct}%. Conviction or comedy? Read the OI before you click buy.",
        "🛸 {sym} +{pct}% in a candle. Either take partial or screenshot for grandkids.",
    ],
    "C2_small": [
        "🟡 {sym} -{pct}%. Shorts ordered popcorn. Decide if you join.",
        "📉 {sym} -{pct}%. Tiny tantrum, big lesson.",
        "🟡 {sym} -{pct}%. Stop is your friend.",
    ],
    "C2_big": [
        "📉 {sym} -{pct}%. The longs are paying for therapy.",
        "🩸 {sym} -{pct}%. SIPs work better than dip-buying drama.",
        "🔴 {sym} -{pct}% — dosa price up, your screen down.",
        "💀 {sym} -{pct}%. Bears having biryani. Are you eating or feeding?",
    ],
    "C2_huge": [
        "🚨 {sym} -{pct}% — capitulation candle. Wait for the bounce, don't catch the knife.",
        "🩸 {sym} -{pct}% in a candle. This is when liquidations happen. Size down.",
        "🚨 {sym} cracked -{pct}%. The right trade is patience.",
    ],
    "C3": [
        "🟡 {sym} drifting -{pct}%. Longs ghosting. No drama, no signal.",
        "⚠️ {sym} -{pct}% on weak conviction. Mostly noise.",
    ],
    "C4": [
        "⚠️ {sym} +{pct}% but OI exiting. Bull trap, na?",
        "🟡 {sym} popped {pct}% — shorts running, longs not buying. Skip.",
        "⚠️ Bear-market rally on {sym}. Thoda calm down.",
    ],
}

# Headline-attached pings (1 in N chance — feel premium without spam)
PN_OF_THE_DAY_PREFIXES = [
    "🏆 PN OF THE DAY",
    "🎯 ONE FOR TODAY",
    "🪙 TODAY'S TRADE",
    "📌 BIG ONE",
]

# Macro hooks — fire when keyword matches known nearby event
MACRO_HOOKS = [
    ("fomc",        "FOMC nearby. {sym} doesn't care, doing {pct}%."),
    ("rate",        "Rate watch on. {sym} {dir_word} regardless."),
    ("cpi",         "CPI just dropped. {sym} {dir_word} {pct}%."),
    ("nfp",         "NFP day. Shorts {state}."),
    ("powell",      "Powell spoke. Translation: {sym} {pct}%."),
    ("earnings",    "Earnings season. {sym} {dir_word} {pct}% — pre-print volatility."),
    ("china",       "China-US chatter again. {sym} {dir_word} {pct}%."),
    ("trump",       "Trump posted. NVDA didn't read it. {sym} did {pct}%."),
    ("election",    "Election noise rising. {sym} {dir_word} {pct}%."),
]

# Seasonal Indian hooks (rotated by date hash)
SEASONAL_HOOKS = {
    # month → list of seasonal one-liners
    1:  ["New year, same charts.", "Resolution: trade smaller in 2026.", "Republic Day next week — markets care less than you."],
    2:  ["Valentines for the bulls?", "Budget done. Trade on.", "ITR season approaching. Logs don't lie."],
    3:  ["Holi week — colour your portfolio carefully.", "FY ending. Book some profits, please.", "March madness, not just basketball."],
    4:  ["IPL is on. Volatility is the real powerplay.", "FY26 begins. Reset stops.", "Earnings season cooking."],
    5:  ["Heat wave outside. Bulls inside.", "Q1 prints incoming.", "May-June is flat, tradition says. Charts disagree."],
    6:  ["Monsoon trade — slippery.", "FOMC summer.", "Half-year done. Audit your trades."],
    7:  ["GST anniversary. Markets unbothered.", "Earnings dump season.", "July rally tradition or trap?"],
    8:  ["Independence week energy.", "Volume thin in August. Be patient.", "Pre-Fed Jackson Hole vibe."],
    9:  ["Festive season approaching. Discipline now.", "Q3 starts strong or doesn't start.", "Septembers are tricky. Respect the tape."],
    10: ["Diwali season — colour your screen green.", "Muhurat trading prep.", "October reversals are real."],
    11: ["Year-end positioning beginning.", "Black Friday cuts coming. So do markets.", "Diwali done, profits booked?"],
    12: ["Santa rally talk. Charts say maybe.", "Year-end window dressing.", "Final week of FY-half. Tax-loss harvesting time."],
}

DISCLAIMERS = [
    "Not financial advice. Just a heads-up.",
    "Probability play, not certainty. Size accordingly.",
    "We don't know your portfolio. You do. Trade smart.",
    "Stops exist for a reason. Use one.",
    "If in doubt, sit it out.",
    "Discipline > conviction.",
]


# ── Pro-trader rule rotation (one-liner reference per PN) ────────
PRO_QUOTES = [
    ("Druckenmiller",  "When you've got it right, size up. When you don't, get out."),
    ("Soros",          "It's not whether you're right or wrong, but how much you make when right."),
    ("Livermore",      "Markets are never wrong; opinions often are."),
    ("Marks (Howard)", "You can't predict, but you can prepare."),
    ("Jhunjhunwala",   "Bull markets begin in pessimism, die in euphoria."),
    ("Tudor Jones",    "The most important rule of trading: play great defense, not great offense."),
    ("Dalio",          "He who lives by the crystal ball is destined to eat broken glass."),
    ("Lynch",          "Know what you own and why you own it."),
    ("Buffett",        "Be fearful when others are greedy."),
    ("Niederhoffer",   "Risk is what's left over when you think you've thought of everything."),
]


# ── Daypart anchors (IST, 24h) ──────────────────────────────────
# Each entry: (hour_start, hour_end, label, [variant headlines])
# These produce *ambient* channel posts — NOT push notifications.
# The 1-PN-per-day budget is reserved for actual signal triggers.
DAYPARTS = [
    (7, 9,   "morning_chai", [
        "☕ Morning. US shut 5h ago. Overnight recap inside.",
        "🌅 Chai time. US tape last said: {recap}.",
        "🌞 Fresh day. Yesterday's S&P closed {sp_dir}. Coffee, then charts.",
    ]),
    (9, 11,  "office_grind", [
        "💼 India open. USD/INR steady. US futures {futures_dir}.",
        "📰 Asian session calm — and the day is long.",
        "⏰ Don't trade pre-US on impulse. Set alerts. Do work.",
    ]),
    (11, 13, "midday_macro", [
        "🍱 Lunch in 1h. EU opens soon. No FOMO trades, please.",
        "📊 Halfway to lunch. India range-bound. US 6h away.",
        "🥗 EU pre-open in 30. Watch DAX for cues.",
    ]),
    (13, 15, "post_lunch", [
        "🍵 Post-lunch slump. So is the chart. Be patient.",
        "📈 India close approaching. Watch FII data.",
        "💤 Most retail loses money in this hour. Don't be most retail.",
    ]),
    (15, 17, "india_close_us_warmup", [
        "🔔 India shutting. Now the real game: US.",
        "☕ Tea break. US futures warming up. Watchlist ready?",
        "📊 India done. 2 hours till the US bell.",
    ]),
    (17, 19, "us_pre_open", [
        "⚡ US opens in 90m. Set stops, set alarms, set discipline.",
        "🎬 Showtime in 1h. Indian retail on US perps — buckle in.",
        "🚦 Pre-market movers: {pre_movers}.",
    ]),
    (19, 20, "us_open", [
        "🇺🇸 US OPEN. First 15min = chaos. Sit. Tight.",
        "🔔 NYSE bell rang. Volatility on tap.",
        "🎯 Open is loud. Real signals come after.",
    ]),
    (20, 22, "early_session", [
        "📊 First hour done. Pattern: {pattern}.",
        "🔍 Now watching: {watch}. Big moves usually post-9:30 ET.",
        "💼 The boring hour. Best for entries on the right setup.",
    ]),
    (22, 24, "dinner_late", [
        "🍽️ Dinner? Markets eat at all hours.",
        "🌙 Late shift. Power-hour in 2h.",
        "👀 Mid-session check: {top_mover}.",
    ]),
    (0, 2,   "power_hour_close", [
        "⏳ Power hour live. Position-mgmt > entries.",
        "🔔 US closes in 30. Booking or holding?",
        "🌙 Bell rings soon. Tomorrow's setup is from tonight's tape.",
    ]),
    (2, 7,   "sleep", []),   # quiet zone — bot stays off the lock-screen
]

# Activity / cultural anchors — fire when the calendar matches
ACTIVITY_HOOKS = {
    "ipl_evening":     "🏏 IPL on tonight. Trade or watch — pick one.",
    "weekend":         "🛋️ Markets shut today. Catch up on Varsity / read 10-Ks.",
    "diwali":          "🪔 Diwali. Stay safe. Tape is thin.",
    "diwali_eve":      "🪔 Diwali eve. Liquidity will vanish. Adjust size.",
    "holi":            "🎨 Holi. Markets closed in India. US still open.",
    "republic_day":    "🇮🇳 Republic Day. Indian markets shut. US tape on.",
    "independence":    "🇮🇳 Independence Day. India shut. US live.",
    "fed_today":       "🏛️ FOMC day. Volatility doubles. Size halves.",
    "cpi_today":       "📊 CPI prints today. Brace for whips.",
    "nfp_today":       "💼 NFP day. First 30 min = no-trade zone.",
    "us_holiday":      "🇺🇸 US holiday today. Tape thin. Skip the chase.",
    "earnings_mega":   "📈 {sym} earnings AMC today. Whole sector will move.",
    "weekend_prep":    "🛠️ Friday close in 2h. Plan Monday. Don't carry weak setups.",
}


# ── Helpers ──────────────────────────────────────────────────────

def _seed(*parts: Any) -> random.Random:
    h = hashlib.md5("|".join(str(p) for p in parts).encode()).hexdigest()
    return random.Random(int(h[:8], 16))


def _bucket(condition_id: str, abs_move: float) -> str:
    if condition_id == "C1":
        return "C1_huge" if abs_move >= 6 else "C1_big" if abs_move >= 2.5 else "C1_small"
    if condition_id == "C2":
        return "C2_huge" if abs_move >= 6 else "C2_big" if abs_move >= 2.5 else "C2_small"
    if condition_id == "C3":
        return "C3"
    return "C4"


def _macro_hook(event_summary: str, sym: str, pct: str, direction: str) -> str | None:
    if not event_summary:
        return None
    lower = event_summary.lower()
    dir_word = "up" if direction == "up" else "down"
    state = "winning" if direction == "down" else "covering"
    for kw, tmpl in MACRO_HOOKS:
        if kw in lower:
            return tmpl.format(sym=sym, pct=pct, dir_word=dir_word, state=state)
    return None


def _seasonal_line(rng: random.Random) -> str:
    today = date.today()
    pool = SEASONAL_HOOKS.get(today.month, [])
    return rng.choice(pool) if pool else ""


def _stars_str(stars: int) -> str:
    return "⭐" * max(0, min(stars, 5))


# ── Public API ───────────────────────────────────────────────────

def _build_one(alert: dict, rng: random.Random, *, force_seasonal: bool) -> dict:
    """Inner builder; returns a single copy dict using the supplied rng."""
    return _build_copy(alert, rng, force_seasonal=force_seasonal)


def generate_pn_variants(alert: dict, n: int = 3) -> list[dict]:
    """
    Return up to n DISTINCT copy variants for the same alert. Useful for
    A/B testing or letting an editor pick. Variants differ by:
      - template choice (within the same condition bucket)
      - whether a macro/seasonal hook is attached
      - which disclaimer line lands at the bottom

    De-duplicates on `headline` so callers always see different headlines.
    """
    sym = alert.get("symbol", "?")
    cid = (alert.get("condition") or {}).get("condition_id", "")
    today_iso = date.today().isoformat()
    seen: set[str] = set()
    out: list[dict] = []
    salts = [None, "alt1", "alt2", "alt3", "alt4", "alt5", "alt6"]
    for salt in salts:
        if len(out) >= n:
            break
        rng = _seed(sym, today_iso, cid, salt)
        force_season = (salt == "alt1")
        copy = _build_one(alert, rng, force_seasonal=force_season)
        if copy["headline"] in seen:
            continue
        seen.add(copy["headline"])
        out.append(copy)
    return out


def generate_pn_copy(alert: dict, *, force_seasonal: bool = False) -> dict:
    """
    Build short, brand-voice push-notification copy from an alert.
    Designed to be the BODY of a Telegram PN; the existing format_alert
    can still be sent as a follow-up "deep dive" message for users
    who want details.
    """
    today_iso = date.today().isoformat()
    cid = (alert.get("condition") or {}).get("condition_id", "")
    rng = _seed(alert.get("symbol", "?"), today_iso, cid)
    return _build_copy(alert, rng, force_seasonal=force_seasonal)


def _build_copy(alert: dict, rng: random.Random, *, force_seasonal: bool) -> dict:
    sym = alert.get("symbol", "?")
    full = alert.get("full_name", sym)
    cond = alert.get("condition") or {}
    cid = cond.get("condition_id", "")
    pt = alert.get("price_trigger") or {}
    move = float(pt.get("price_change_pct") or 0.0)
    direction = "up" if move >= 0 else "down"
    abs_pct = abs(move)
    pct_str = f"{abs_pct:.1f}"

    score = int(alert.get("score") or 0)
    stars = int(alert.get("stars") or 0)
    is_pn_today = bool(alert.get("pn_today") or alert.get("is_pn"))

    # Pick core template
    bucket = _bucket(cid, abs_pct)
    tmpl = rng.choice(TEMPLATES.get(bucket, TEMPLATES["C1_small"]))
    line = tmpl.format(sym=sym, pct=pct_str, full=full)

    # Optional macro hook (events context)
    event_ctx = alert.get("event_context") or {}
    macro_events = event_ctx.get("macro_events_soon") or []
    macro_blob = " ".join((m.get("event") or "") for m in macro_events)
    earnings = event_ctx.get("next_earnings") or {}
    if earnings:
        macro_blob += " earnings"
    macro_line = _macro_hook(macro_blob, sym, pct_str, direction) if macro_blob else None

    # Seasonal sprinkle (1 in 3 chance unless forced)
    seasonal = _seasonal_line(rng) if (force_seasonal or rng.random() < 0.33) else ""

    # PN-of-the-day marker
    pn_prefix = ""
    if is_pn_today or score >= 88:
        pn_prefix = rng.choice(PN_OF_THE_DAY_PREFIXES) + " · "

    # Headline assembly
    headline = (pn_prefix + line).strip()
    if len(headline) > 110:
        headline = headline[:107] + "…"

    # Body lines
    body_bits = []
    if seasonal:
        body_bits.append(f"<i>{seasonal}</i>")
    if macro_line:
        body_bits.append(f"🌐 {macro_line}")
    earnings_dte = event_ctx.get("days_to_earnings")
    if earnings_dte is not None and earnings_dte <= 7:
        body_bits.append(f"🗓️ Earnings in {earnings_dte}d.")
    if stars:
        body_bits.append(f"<b>{cid}</b> · score <code>{score}/100</code> · {_stars_str(stars)}")
    elif score:
        body_bits.append(f"<b>{cid}</b> · score <code>{score}/100</code>")

    # Trade plan one-liner (ATR if available, else %)
    tech = alert.get("technical_outlook") or {}
    atr = float(tech.get("atr") or 0)
    price = float(pt.get("current_price") or 0)
    if atr and price:
        if direction == "up":
            body_bits.append(
                f"🎯 stop ${price - 1.5*atr:.2f} · TP ${price + 2*atr:.2f} · max 2-3% capital"
            )
        else:
            body_bits.append(
                f"🎯 stop ${price + 1.5*atr:.2f} · TP ${price - 2*atr:.2f} · max 2-3% capital"
            )
    else:
        body_bits.append("🎯 stop -1.5% · TP +2% · max 2-3% capital")

    # Disclaimer (deterministic per day so user doesn't see same line back-to-back
    # within a single batch but rotates across days)
    body_bits.append(f"<i>{rng.choice(DISCLAIMERS)}</i>")

    body = "\n".join(body_bits)
    hashtags = f"#{sym} #{cid} #PN" + (" #INDIA" if seasonal else "")

    return {
        "headline": headline,
        "body": body,
        "hashtags": hashtags,
        "full": f"<b>{headline}</b>\n\n{body}\n\n<code>{hashtags}</code>",
    }


# ── Convenience: format the existing alert dict the brand way ────

def format_pn(alert: dict) -> str:
    """Drop-in replacement for the verbose format_alert when in PN mode."""
    return generate_pn_copy(alert)["full"]


# ── Trade-reference card ─────────────────────────────────────────

def _historical_pattern_stats(sym: str, condition_id: str) -> dict | None:
    """
    Best-effort lookup of recent same-(ticker, condition) outcomes from
    analysis/report.json, if it exists. Returns {n, wins, win_rate_pct}
    or None when the report isn't available or has no matching rows.
    """
    import json as _json
    from pathlib import Path as _P
    p = _P(__file__).resolve().parent.parent / "analysis" / "report.json"
    if not p.exists():
        return None
    try:
        rep = _json.loads(p.read_text())
    except Exception:
        return None
    matches = [
        s for s in rep.get("signals", [])
        if s.get("ticker") == sym and s.get("condition_id") == condition_id
        and s.get("outcome") in ("tp1", "sl", "timeout")
    ]
    if not matches:
        return None
    wins = sum(1 for s in matches if (s.get("pnl_pct") or 0) > 0)
    return {
        "n": len(matches),
        "wins": wins,
        "win_rate_pct": round(wins / len(matches) * 100, 0),
    }


def _build_trade_card(alert: dict, rng: random.Random) -> str:
    """
    Render a concrete TRADE CARD with entry zone, multi-target TPs,
    R:R per target, position-sizing guidance, trend / volume
    confirmation, historical pattern win rate, and a rotating
    pro-trader quote.

    Returns an HTML-formatted string suitable for direct concat
    into the PN body.
    """
    sym = alert.get("symbol", "?")
    cond = alert.get("condition") or {}
    cid = cond.get("condition_id", "")
    pt = alert.get("price_trigger") or {}
    price = float(pt.get("current_price") or 0)
    move = float(pt.get("price_change_pct") or 0)
    long_side = move >= 0  # C1/C4 → long, C2/C3 → short (best-effort)

    tech = alert.get("technical_outlook") or {}
    atr = float(tech.get("atr") or 0)
    if atr <= 0 and price > 0:
        atr = price * 0.01  # 1% fallback
    e20 = tech.get("ema20"); e50 = tech.get("ema50"); e200 = tech.get("ema200")
    rsi_v = tech.get("rsi")

    # Entry zone — half-ATR around current
    if long_side:
        entry_lo = price - 0.3 * atr
        entry_hi = price + 0.1 * atr
        sl       = price - 1.5 * atr
        tp1      = price + 1.0 * atr
        tp2      = price + 2.0 * atr
        tp3      = price + 3.5 * atr
    else:
        entry_lo = price - 0.1 * atr
        entry_hi = price + 0.3 * atr
        sl       = price + 1.5 * atr
        tp1      = price - 1.0 * atr
        tp2      = price - 2.0 * atr
        tp3      = price - 3.5 * atr

    risk = abs(price - sl)
    rr = lambda t: round(abs(t - price) / risk, 2) if risk > 0 else 0.0
    pct = lambda t: ((t - price) / price * 100) if long_side else ((price - t) / price * 100)

    # Trend confirm
    trend_line = "—"
    if e20 and e50 and e200:
        if e20 > e50 > e200 and price > e20:
            trend_line = "🟢 above EMA20 &gt; 50 &gt; 200 — trend intact"
        elif e20 < e50 < e200 and price < e20:
            trend_line = "🔴 below EMA20 &lt; 50 &lt; 200 — downtrend intact"
        else:
            trend_line = "🟡 mixed EMAs — trend unclear"
    rsi_line = f"RSI {rsi_v:.0f}" if isinstance(rsi_v, (int, float)) else "RSI n/a"

    # Volume confirm — use 24h vol from oi_report when present
    oi = alert.get("oi_report") or {}
    vol24 = float(oi.get("volume_24h") or 0)
    vol_line = (f"24h ${vol24/1e6:.1f}M" if vol24 else "vol n/a")

    # Historical reference
    hist = _historical_pattern_stats(sym, cid)
    if hist:
        hist_line = (f"📚 Past {hist['n']}× {sym} {cid} — "
                      f"{hist['wins']}W/{hist['n']-hist['wins']}L "
                      f"<b>{hist['win_rate_pct']:.0f}%</b>")
    else:
        hist_line = "📚 No prior {sym} {cid} samples in window".format(sym=sym, cid=cid)

    quote = rng.choice(PRO_QUOTES)
    quote_line = f"<i>“{quote[1]}”</i> — {quote[0]}"

    # Position sizing snippet (1% account risk → contracts/qty depends on price+stop)
    # We can't know account size; surface the rule + the per-share risk in $.
    per_share_risk = round(abs(price - sl), 2)
    pos_line = (f"⚖️ Risk 1% of account · per-share risk <b>${per_share_risk:.2f}</b> "
                f"({per_share_risk/price*100:.1f}%)")

    side_emoji = "🟢 LONG" if long_side else "🔴 SHORT"
    card = (
        f"\n<b>📋 TRADE CARD · {side_emoji}</b>\n"
        f"Entry zone: <b>${entry_lo:.2f} – ${entry_hi:.2f}</b>\n"
        f"Stop loss:  <b>${sl:.2f}</b> ({pct(sl):+.2f}%)\n"
        f"TP1: ${tp1:.2f} ({pct(tp1):+.2f}% · R:R {rr(tp1)})\n"
        f"TP2: ${tp2:.2f} ({pct(tp2):+.2f}% · R:R {rr(tp2)})\n"
        f"TP3: ${tp3:.2f} ({pct(tp3):+.2f}% · R:R {rr(tp3)})\n"
        f"Time horizon: <b>intraday 1–4h</b> · trail TP3 if hit\n"
        f"{pos_line}\n"
        f"📊 Trend: {trend_line} · {rsi_line} · vol {vol_line}\n"
        f"{hist_line}\n"
        f"{quote_line}"
    )
    return card


def generate_pn_with_card(alert: dict, *, force_seasonal: bool = False) -> dict:
    """
    Same as generate_pn_copy but appends a concrete TRADE CARD with
    entry/exit levels, R:R, sizing guidance, trend confirmation,
    historical stats, and a rotating pro-trader quote.
    """
    today_iso = date.today().isoformat()
    cid = (alert.get("condition") or {}).get("condition_id", "")
    rng = _seed(alert.get("symbol", "?"), today_iso, cid)
    base = _build_copy(alert, rng, force_seasonal=force_seasonal)
    card = _build_trade_card(alert, rng)
    base["trade_card"] = card
    base["full"] = base["full"] + card
    return base


def generate_pn_variants_with_card(alert: dict, n: int = 3) -> list[dict]:
    """Variants of generate_pn_variants() that each include a trade card.
    The trade card is the SAME across variants (data is data) — only the
    headline / body voice differs."""
    base_variants = generate_pn_variants(alert, n=n)
    today_iso = date.today().isoformat()
    cid = (alert.get("condition") or {}).get("condition_id", "")
    rng = _seed(alert.get("symbol", "?"), today_iso, cid, "card")
    card = _build_trade_card(alert, rng)
    out = []
    for v in base_variants:
        v2 = dict(v)
        v2["trade_card"] = card
        v2["full"] = v["full"] + card
        out.append(v2)
    return out


# ── Daypart / activity copy ──────────────────────────────────────

def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Sat=5, Sun=6


def _ipl_window(d: date) -> bool:
    # IPL season is approx late March through end of May in most years
    return d.month in (3, 4, 5) and d.weekday() in (1, 2, 4, 5, 6)


def daypart_copy_for(dt_ist: datetime, *, recap: str = "", sp_dir: str = "—",
                     futures_dir: str = "flat", pre_movers: str = "—",
                     pattern: str = "—", watch: str = "—",
                     top_mover: str = "—") -> dict | None:
    """
    Pick the daypart anchor variant for a given IST datetime. Returns
    a dict {label, headline, body} or None for the sleep window.
    """
    h = dt_ist.hour
    for start, end, label, variants in DAYPARTS:
        if start <= h < end:
            if not variants:
                return None
            rng = _seed("daypart", dt_ist.date().isoformat(), label)
            tmpl = rng.choice(variants)
            headline = tmpl.format(recap=recap, sp_dir=sp_dir, futures_dir=futures_dir,
                                    pre_movers=pre_movers, pattern=pattern,
                                    watch=watch, top_mover=top_mover)
            return {"label": label, "headline": headline}
    return None


def activity_hooks_for(d: date, *, fed_today: bool = False, cpi_today: bool = False,
                        nfp_today: bool = False, mega_earnings: list[str] | None = None,
                        us_holiday: bool = False) -> list[str]:
    """Return a list of activity-anchor lines that apply to date d."""
    hooks: list[str] = []
    if _is_weekend(d):
        hooks.append(ACTIVITY_HOOKS["weekend"])
    if d.weekday() == 4:  # Friday
        hooks.append(ACTIVITY_HOOKS["weekend_prep"])
    if d.month == 1 and d.day == 26:
        hooks.append(ACTIVITY_HOOKS["republic_day"])
    if d.month == 8 and d.day == 15:
        hooks.append(ACTIVITY_HOOKS["independence"])
    if fed_today: hooks.append(ACTIVITY_HOOKS["fed_today"])
    if cpi_today: hooks.append(ACTIVITY_HOOKS["cpi_today"])
    if nfp_today: hooks.append(ACTIVITY_HOOKS["nfp_today"])
    if us_holiday: hooks.append(ACTIVITY_HOOKS["us_holiday"])
    if _ipl_window(d):
        hooks.append(ACTIVITY_HOOKS["ipl_evening"])
    for sym in (mega_earnings or []):
        hooks.append(ACTIVITY_HOOKS["earnings_mega"].format(sym=sym))
    return hooks
