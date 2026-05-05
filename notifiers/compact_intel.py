# notifiers/compact_intel.py
# ─────────────────────────────────────────────────────────────────
# Generate compact PN bodies from LIVE INTELLIGENCE — no hardcoded
# templates. Each body is composed from quantified signals visible
# at fire time:
#
#   • Move direction + magnitude
#   • OI direction (proxied by funding sign when no history)
#   • Volume z-score vs N-bar baseline
#   • ATR ratio vs 20-bar baseline
#   • Cluster narrative (broad risk-on/off vs idio)
#   • Recent news headline (when Finnhub key valid)
#
# Output is < 80 chars (the spec). Sentences are short, action
# verbs trailing. Composition rule:
#
#   FACT_1 · FACT_2. ACTION.
#
# Where each FACT comes from real data (not lookup table) and is
# ranked by how surprising it is vs the per-ticker baseline.
# ─────────────────────────────────────────────────────────────────

from __future__ import annotations

import json
import logging
import math
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

BODY_MAX = 80


@dataclass
class IntelInputs:
    symbol: str
    move_pct: float
    condition_id: str = ""           # C1/C2/C3/C4 if known
    funding: float = 0.0             # signed funding rate
    oi_change_pct: float | None = None  # if available
    volume_zscore: float | None = None  # vs 20-bar baseline
    atr_ratio: float | None = None      # current_atr / 20-bar atr
    near_vwap: str | None = None        # "above" / "below" / None
    range_compression: bool = False     # tight bars before
    news_headline: str | None = None    # latest catalyst line
    cluster_tilt: str | None = None     # "broad_off"/"broad_on"/"idio"/None


# ── Live-data fetchers ──────────────────────────────────────────

def _hl_post(payload: dict, timeout: int = 8) -> list | None:
    try:
        r = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://api.hyperliquid.xyz/info",
             "-H", "Content-Type: application/json",
             "-d", json.dumps(payload)],
            capture_output=True, text=True, timeout=timeout,
        )
        return json.loads(r.stdout)
    except Exception:
        return None


def fetch_intel_signals(symbol: str, hl_asset: str | None = None,
                        bars: int = 24) -> dict:
    """
    Pull last N 15-min bars from HL and derive volume z-score,
    ATR ratio, and VWAP-relative position.
    """
    coin = hl_asset or f"xyz:{symbol}"
    now_ms = int(time.time() * 1000)
    cs = _hl_post({"type": "candleSnapshot", "req": {
        "coin": coin, "interval": "15m",
        "startTime": now_ms - (bars + 4) * 15 * 60 * 1000,
        "endTime": now_ms,
    }})
    if not isinstance(cs, list) or len(cs) < 5:
        return {}

    closes = [float(c["c"]) for c in cs]
    highs = [float(c["h"]) for c in cs]
    lows = [float(c["l"]) for c in cs]
    vols = [float(c["v"]) for c in cs]
    cur = closes[-1]

    # Volume z-score (current vs 20-bar mean / stdev)
    vol_z = None
    if len(vols) >= 6:
        prior = vols[:-1]
        mean = sum(prior) / len(prior)
        var = sum((v - mean) ** 2 for v in prior) / max(len(prior) - 1, 1)
        std = math.sqrt(var) if var > 0 else 0
        if std > 0:
            vol_z = round((vols[-1] - mean) / std, 1)

    # ATR ratio (last bar TR vs 20-bar avg TR)
    atr_ratio = None
    if len(cs) >= 6:
        trs = []
        for i in range(1, len(cs)):
            tr = max(highs[i] - lows[i],
                      abs(highs[i] - closes[i - 1]),
                      abs(lows[i] - closes[i - 1]))
            trs.append(tr)
        avg_tr = sum(trs[:-1]) / max(len(trs) - 1, 1)
        if avg_tr > 0:
            atr_ratio = round(trs[-1] / avg_tr, 1)

    # VWAP-relative
    near_vwap = None
    typ = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(cs))]
    pv = sum(t * v for t, v in zip(typ, vols))
    sv = sum(vols) or 1
    vwap = pv / sv
    if cur > vwap * 1.002:
        near_vwap = "above"
    elif cur < vwap * 0.998:
        near_vwap = "below"

    # Range compression: last 4 bars max-min < 0.5 × 20-bar avg range
    range_compression = False
    if len(cs) >= 8:
        recent_range = max(highs[-4:]) - min(lows[-4:])
        all_ranges = [highs[i] - lows[i] for i in range(len(cs))]
        avg_range = sum(all_ranges) / len(all_ranges)
        if avg_range > 0 and recent_range < 0.5 * avg_range:
            range_compression = True

    return {
        "volume_zscore": vol_z,
        "atr_ratio": atr_ratio,
        "near_vwap": near_vwap,
        "range_compression": range_compression,
        "current_price": cur,
        "vwap": round(vwap, 2),
    }


# ── Body composer ───────────────────────────────────────────────

def _facts(inp: IntelInputs) -> list[str]:
    """Order by how surprising each fact is. Each must be < 30 chars."""
    facts: list[str] = []

    # Volume spike — the loudest signal
    if inp.volume_zscore is not None and inp.volume_zscore >= 2.5:
        facts.append(f"Vol {inp.volume_zscore:.1f}σ")
    elif inp.volume_zscore is not None and inp.volume_zscore >= 1.5:
        facts.append(f"Vol +{inp.volume_zscore:.1f}σ")

    # OI / funding (whichever is cleaner)
    if inp.oi_change_pct is not None and abs(inp.oi_change_pct) >= 1.5:
        sign = "+" if inp.oi_change_pct > 0 else ""
        facts.append(f"OI {sign}{inp.oi_change_pct:.1f}%")
    elif inp.funding != 0:
        bps = inp.funding * 10000
        if abs(bps) >= 0.5:
            facts.append(f"Fund {bps:+.1f}bp")

    # ATR expansion
    if inp.atr_ratio is not None:
        if inp.atr_ratio >= 2.0:
            facts.append(f"ATR {inp.atr_ratio:.1f}×")
        elif inp.atr_ratio <= 0.5:
            facts.append("Coiling")

    # VWAP context
    if inp.near_vwap == "above":
        facts.append("Above VWAP")
    elif inp.near_vwap == "below":
        facts.append("Below VWAP")

    # Range squeeze
    if inp.range_compression:
        facts.append("Squeeze")

    # Cluster narrative
    if inp.cluster_tilt == "broad_off":
        facts.append("Tape wide")
    elif inp.cluster_tilt == "broad_on":
        facts.append("Tape wide")
    elif inp.cluster_tilt == "idio":
        facts.append("Idio")

    return facts


def _action(inp: IntelInputs) -> str:
    """Single short verb tied to setup."""
    cid = (inp.condition_id or "").upper()
    if cid == "C1":  return "Buy"
    if cid == "C2":  return "Short"
    if cid == "C3":  return "Wait"
    if cid == "C4":  return "Fade"
    if inp.move_pct > 0.5:  return "Buy"
    if inp.move_pct < -0.5: return "Short"
    return "Wait"


def build_intel_body(inp: IntelInputs) -> str:
    """
    Compose a body string under 80 chars from live intelligence.
    Format: "FACT · FACT · FACT. ACTION."
    """
    facts = _facts(inp)

    # Add news headline lead if available (truncated tight)
    news_lead = ""
    if inp.news_headline:
        cleaned = inp.news_headline.strip().rstrip(".")
        if len(cleaned) > 38:
            cleaned = cleaned[:37] + "…"
        news_lead = cleaned

    action = _action(inp)

    # Pack until under 80 chars
    parts: list[str] = []
    if news_lead:
        parts.append(news_lead)
    for f in facts:
        candidate = " · ".join(parts + [f])
        # Reserve ~10 chars for ". <action>."
        if len(candidate) > BODY_MAX - 10:
            break
        parts.append(f)

    if not parts:
        # Pure-direction fallback (still data-driven)
        parts.append(f"{inp.move_pct:+.1f}% move")

    body = " · ".join(parts).rstrip(". ") + f". {action}."

    # Hard guard
    if len(body) > BODY_MAX:
        body = body[: BODY_MAX - 1].rstrip(" ,.;:") + "."
    return body


# ── Stage-specific composers ────────────────────────────────────

def build_stage_break(inp: IntelInputs, intel: dict) -> dict:
    """T+0 — first move, immediate setup."""
    inp.volume_zscore = inp.volume_zscore or intel.get("volume_zscore")
    inp.atr_ratio = inp.atr_ratio or intel.get("atr_ratio")
    inp.near_vwap = inp.near_vwap or intel.get("near_vwap")
    inp.range_compression = inp.range_compression or intel.get("range_compression", False)
    body = build_intel_body(inp)
    emoji = "🔴" if inp.move_pct < -0.3 else ("🟢" if inp.move_pct > 0.3 else "⚪")
    catalyst = (inp.news_headline or "Move").strip()
    if len(catalyst) > 22: catalyst = catalyst[:21] + "…"
    title = f"{emoji} {catalyst} · {inp.symbol} {inp.move_pct:+.1f}%"
    if len(title) > 50: title = title[:49] + "…"
    return {"stage": "break", "title": title, "body": body,
             "total_chars": len(title) + len(body) + 1}


def build_stage_cascade(inp: IntelInputs, intel: dict, *,
                          liquidation_usd_m: float | None = None) -> dict:
    """T+15 — follow-through with cascade context."""
    inp.volume_zscore = inp.volume_zscore or intel.get("volume_zscore")
    inp.atr_ratio = inp.atr_ratio or intel.get("atr_ratio")
    inp.near_vwap = inp.near_vwap or intel.get("near_vwap")

    # Cascade title prioritises liquidation when present
    if liquidation_usd_m and liquidation_usd_m >= 5:
        emoji = "💀" if inp.move_pct < 0 else "🚀"
        title = f"{emoji} ${liquidation_usd_m:.0f}M liq · {inp.symbol} {inp.move_pct:+.1f}%"
    else:
        emoji = "📉" if inp.move_pct < 0 else "📈"
        vwap = intel.get("vwap")
        if vwap:
            title = f"{emoji} {inp.symbol} ${vwap:.0f} VWAP · {inp.move_pct:+.1f}%"
        else:
            title = f"{emoji} {inp.symbol} cascade · {inp.move_pct:+.1f}%"
    if len(title) > 50: title = title[:49] + "…"
    body = build_intel_body(inp)
    return {"stage": "cascade", "title": title, "body": body,
             "total_chars": len(title) + len(body) + 1}


def build_stage_repricing(inp: IntelInputs, intel: dict, *,
                            next_event: str | None = None,
                            net_move_pct: float | None = None) -> dict:
    """T+2hr — equilibrium + next setup."""
    inp.volume_zscore = inp.volume_zscore or intel.get("volume_zscore")
    inp.atr_ratio = inp.atr_ratio or intel.get("atr_ratio")
    if net_move_pct is not None:
        inp.move_pct = net_move_pct  # body should reflect settled level

    if net_move_pct is not None:
        title = f"📊 {inp.symbol} settled · {net_move_pct:+.1f}% net"
    else:
        title = f"📊 {inp.symbol} repriced · equilibrium"
    if len(title) > 50: title = title[:49] + "…"

    body_parts = []
    facts = _facts(inp)
    if facts:
        body_parts.extend(facts[:2])
    if next_event:
        nxt = next_event.strip()
        if len(nxt) > 24: nxt = nxt[:23] + "…"
        body_parts.append(f"Next: {nxt}")
    if not body_parts:
        body_parts.append(f"{net_move_pct or inp.move_pct:+.1f}% net")
    body = " · ".join(body_parts) + ". Watch."
    if len(body) > BODY_MAX:
        body = body[: BODY_MAX - 1].rstrip(" ,.;:") + "."
    return {"stage": "repricing", "title": title, "body": body,
             "total_chars": len(title) + len(body) + 1}


# ── Convenience: build whole wave from minimal inputs ───────────

def build_wave_intel(*, symbol: str, hl_asset: str, move_pct: float,
                     condition_id: str = "",
                     funding: float = 0.0, oi_change_pct: float | None = None,
                     news_headline: str | None = None,
                     liquidation_usd_m: float | None = None,
                     next_event: str | None = None,
                     cluster_tilt: str | None = None) -> list[dict]:
    intel = fetch_intel_signals(symbol, hl_asset)
    inp = IntelInputs(
        symbol=symbol, move_pct=move_pct, condition_id=condition_id,
        funding=funding, oi_change_pct=oi_change_pct,
        news_headline=news_headline, cluster_tilt=cluster_tilt,
    )
    return [
        build_stage_break(inp, intel),
        build_stage_cascade(inp, intel, liquidation_usd_m=liquidation_usd_m),
        build_stage_repricing(inp, intel, next_event=next_event,
                                net_move_pct=move_pct),  # caller can override later
    ]


if __name__ == "__main__":
    # Demo on a real ticker
    intel = fetch_intel_signals("AAPL", "xyz:AAPL")
    print("Intel:", intel)
    inp = IntelInputs(symbol="AAPL", move_pct=-1.2, condition_id="C2",
                       funding=0.00045, oi_change_pct=2.1,
                       news_headline="UBS downgrades AAPL on tariff risk",
                       cluster_tilt="broad_off")
    inp.volume_zscore = intel.get("volume_zscore")
    inp.atr_ratio = intel.get("atr_ratio")
    inp.near_vwap = intel.get("near_vwap")
    print("\nBody:", build_intel_body(inp))
    print(f"({len(build_intel_body(inp))} chars)")
